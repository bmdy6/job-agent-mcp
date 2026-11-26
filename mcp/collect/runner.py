"""Coordinate JobSource calls. Browser driver and SQLite are opaque interfaces."""
import random
import logging
import time
from browser.base import Browser, BrowserError, SpaceUnavailable
from platforms.base import JobSource, SearchQuery
from .filters import filter_reasons
from .deadline import platform_deadline
from engine.rate import RateDeferred


log = logging.getLogger(__name__)


class Collector:
    def __init__(self, browser: Browser, sources: list[JobSource], repository, settings, *,
                 clock=time.time, sleep=time.sleep, alert=lambda *args: None, safety=None, limiter=None, seed=None):
        self.browser, self.sources, self.repo, self.settings = browser, sources, repository, settings
        self.clock, self.sleep, self.alert = clock, sleep, alert
        self.repo.clock = clock
        self.random=random.Random(seed)
        self._due={}
        self.safety,self.limiter=safety,limiter

    def _visit(self, platform):
        if self.limiter is not None:
            self.limiter.acquire(platform,'collect',self.settings.rate_policy(platform),block=False)
            self.repo.visited(platform)
            return
        last = self.repo.last_visit(platform)
        if platform not in self._due:
            self._due[platform]=0 if last is None else last+self.random.uniform(*self.settings.interval_seconds)
        if self.clock()<self._due[platform]: raise RateDeferred('cooldown')
        self.repo.visited(platform)
        self._due[platform]=self.clock()+self.random.uniform(*self.settings.interval_seconds)

    def _platform(self, source, space, result):
        name = source.platform
        self.repo.begin_cycle(name, self.settings.keywords, self.settings.cities, refresh_daily=self.settings.refresh_daily)
        checked_details = set()
        limit = self.settings.platforms[name].daily_page_limit
        for keyword in self.settings.keywords:
            for city in self.settings.cities:
                while True:
                    page = self.repo.next_page(name, keyword, city)
                    if page is None:
                        break
                    query = SearchQuery(keyword, city, page, self.settings.filters.salary_min_k)
                    cost = source.search_page_cost(query) if hasattr(source, 'search_page_cost') else 1
                    if not self.repo.reserve_page(name, limit, cost):
                        result['limited'] = True
                        return
                    yield
                    result['pages'] += cost
                    try:
                        steps=None
                        if hasattr(source, 'search_steps'):
                            steps=source.search_steps(space,query)
                            while True:
                                try: next(steps)
                                except StopIteration as done:
                                    listings=done.value
                                    break
                                yield
                        else:
                            listings = source.search(space, query)
                    finally:
                        if steps is not None: steps.close()
                        # Internal pagination may have visited again after _visit.
                        self.repo.visited(name)
                    result['skipped_rows'] = result.get('skipped_rows', 0) + getattr(source, 'skipped_rows', 0)
                    self.repo.error(name, '')
                    pending_details = {}
                    for listing in listings:
                        if listing.platform != name or not listing.platform_job_id:
                            raise ValueError('JobSource returned invalid identity')
                        reasons = filter_reasons(listing, self.settings.filters)
                        existed = self.repo.contains(listing)
                        job = self.repo.ingest(listing, reasons)
                        result['new' if not existed else 'refreshed'] += 1
                        result['jobs'] += 1
                        result['filtered'] += bool(reasons)
                        if self.settings.fetch_jd and (not reasons or self.settings.refresh_daily) and listing.platform_job_id not in checked_details and (not job['jd'] or not job['hr_name']):
                            pending_details[listing.platform_job_id] = listing
                    for listing in pending_details.values():
                        checked_details.add(listing.platform_job_id)
                        yield
                        jd = source.detail(space, listing)
                        if jd:
                            self.repo.set_jd(listing, jd)
                            result['details'] += 1
                    self.repo.complete(name, query, done=not listings or self.settings.refresh_daily)
                    if not listings or self.settings.refresh_daily:
                        break

    def run(self):
        result = {'platforms': {}, 'failed': 0}
        active = {}
        def failed(name, stats, error):
            code=getattr(error,'code',type(error).__name__)
            if code in {'risk_control','unexpected_redirect'} and self.safety is not None:
                self.safety.repo.pause(name,code)
            elif code=='risk_control':
                self.repo.store.pause_platform(name,code)
            stats['error']=code
            if getattr(error,'alert_failed',False): stats['alert_failed']=True
            result['failed']+=1
            self.repo.error(name,code)
            try:
                if not isinstance(error,RateDeferred) or code=='risk_control': self.alert(name,code)
            except Exception: stats['alert_failed']=True
        with self.repo.exclusive() as acquired:
            if not acquired: return {**result,'busy':True}
            try:
                for source in self.sources:
                    name=source.platform
                    options=self.settings.platforms.get(name)
                    if not options or not options.enabled: continue
                    stats=result['platforms'][name]=dict(pages=0,jobs=0,new=0,refreshed=0,filtered=0,details=0)
                    if name in self.repo.store.paused_platforms():
                        stats['error']='risk_control_paused'
                        result['failed']+=1
                        continue
                    started=time.monotonic()
                    try:
                        with platform_deadline(self.settings.platform_timeout_seconds):
                            if self.safety is not None: self.safety.require(name)
                            space=self.browser.space(self.settings.task_space_name)
                            gen=self._platform(source,space,stats)
                            next(gen)
                            active[name]=(gen,stats,self.settings.platform_timeout_seconds-(time.monotonic()-started))
                    except StopIteration: pass
                    except Exception as error:
                        failed(name,stats,error)
                        if isinstance(error,SpaceUnavailable):
                            result['stopped']=True
                            return result
                while active:
                    order=list(active)
                    self.random.shuffle(order)
                    progressed=False
                    for name in order:
                        gen,stats,budget=active[name]
                        started=time.monotonic()
                        try:
                            if budget<=0:
                                from .deadline import CollectionTimeout
                                raise CollectionTimeout('Platform collection timed out')
                            with platform_deadline(budget):
                                self._visit(name)
                                next(gen)
                            active[name]=(gen,stats,budget-(time.monotonic()-started))
                            progressed=True
                        except StopIteration:
                            active.pop(name)
                            progressed=True
                        except RateDeferred as error:
                            if error.code=='cooldown':
                                active[name]=(gen,stats,budget-(time.monotonic()-started)-.25)
                                continue
                            failed(name,stats,error)
                            gen.close()
                            active.pop(name)
                        except Exception as error:
                            failed(name,stats,error)
                            gen.close()
                            active.pop(name)
                            if isinstance(error,SpaceUnavailable):
                                result['stopped']=True
                                return result
                    if active and not progressed: self.sleep(.25)
            finally:
                for gen,_,_ in active.values(): gen.close()
        return result
