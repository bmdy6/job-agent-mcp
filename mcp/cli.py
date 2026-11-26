"""job-agent CLI for owned services and saved-reply notifications."""
import argparse
import json
import logging
import time

from config import load_config
from watch import poll_once

log = logging.getLogger(__name__)


def _setup_logging(command):
    """Rotate INFO logs under $JOB_AGENT_HOME/logs; fall back to stderr when config is unusable."""
    from logsetup import setup_logging
    try:
        from config import load_config, job_agent_home
        obs = load_config().observability
        setup_logging(command, home=job_agent_home(), max_bytes=obs.log_max_bytes, backups=obs.log_backups)
    except Exception:
        setup_logging(command)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='job-agent')
    sub = parser.add_subparsers(dest='command', required=True)
    resume = sub.add_parser('resume-platform', help='Clear a risk pause after the user resolves the original site; never sends')
    resume.add_argument('platform',choices=('boss','zhilian','liepin','51job','nowcoder', 'maimai'))
    collect = sub.add_parser('collect', help='Collect read-only using JobSource and a dedicated Ego Lite space')
    collect.add_argument('--once', action='store_true', required=True, help='Run one resumable collection pass')
    collect.add_argument('--platform', action='append', choices=('boss', 'zhilian', 'liepin', '51job', 'nowcoder', 'maimai'))
    collect.add_argument('--dry-run', action='store_true', help='Fake sources and a separate SQLite database; no browser or notifications')
    daily = sub.add_parser('daily', help='Run an explicitly authorized daily collection/delivery round')
    daily.add_argument('--once', action='store_true', required=True)
    daily.add_argument('--collection-page-limit', type=int, choices=range(1,21), help='Temporary lower collection page ceiling for acceptance')
    daily.add_argument('--max-per-platform', type=int, help='Acceptance-only cap, 1..3 jobs per platform')
    audit = sub.add_parser('verify-receipt', help='Read the original site for one uncertain application; never submits or rewrites history')
    audit.add_argument('--job-id', type=int, required=True, help='Owned database row ID of the uncertain attempt')
    reconcile51 = sub.add_parser('reconcile-51job', help='Read official 51job applications and reconcile local evidence; never submits or writes history')
    reconcile51.add_argument('--database', help='Owned database to open read-only')
    receipt_status = sub.add_parser('resume-status', help='Read local resume evidence and the old-job review list; never sends')
    receipt_status.add_argument('--before', help='Only jobs collected before YYYY-MM-DD (China time)')
    receipt_status.add_argument('--include-jobs', action='store_true')
    receipt_status.add_argument('--limit', type=int, default=200)
    receipt_status.add_argument('--database', help='Owned database to open read-only')
    local_review = sub.add_parser('review-resume-record', help='Review a supplied application record against one owned job; no website access')
    local_review.add_argument('--job-id', type=int, required=True)
    local_review.add_argument('--record-file', required=True, help='Local JSON record obtained through the normal official UI')
    local_review.add_argument('--database', help='Owned database to open read-only')
    heartbeat = sub.add_parser('heartbeat', help='Alert once when the monitor or daily run goes quiet; never opens a browser')
    heartbeat.add_argument('--once', action='store_true', required=True)
    dashboard = sub.add_parser('dashboard', help='Generate one private offline HTML from the read-only local database')
    dashboard.add_argument('--out', help='Output HTML; default $JOB_AGENT_HOME/dashboard.html')
    watch = sub.add_parser('watch', help='Notify configured Telegram channels from owned saved replies')
    watch.add_argument('--once', action='store_true', help='Poll once and exit; default is every configured N minutes')
    watch.add_argument('--source', choices=('replies',), default='replies')
    monitor = sub.add_parser('monitor', help='Poll platform replies read-only using a dedicated Ego Lite space')
    monitor.add_argument('--once', action='store_true')
    deliver = sub.add_parser('deliver', help='Preview and confirm an isolated delivery batch')
    action = deliver.add_mutually_exclusive_group(required=True)
    action.add_argument('--preview', action='store_true')
    action.add_argument('--confirm', metavar='TOKEN')
    deliver.add_argument('--database', required=True, help='Explicit isolated database path for dry-run')
    deliver.add_argument('--job-ids', type=int, nargs='+', help='Owned database row IDs for preview')
    deliver.add_argument('--notify', action='store_true', help='Explicit Telegram opt-in; forbidden for dry-run')
    args = parser.parse_args(argv)
    if args.command == 'dashboard':
        from service.dashboard import generate
        try:
            print(json.dumps({'path': generate(args.out)}, ensure_ascii=False))
            return 0
        except Exception as error:
            # No log/config/runtime writes; errors expose type only.
            import sys
            print('dashboard failed: '+type(error).__name__, file=sys.stderr)
            return 1
    _setup_logging(args.command)
    if args.command == 'heartbeat':
        import config as config_module
        from service.heartbeat import run_once, notifier
        settings = config_module.load_config()
        print(json.dumps(run_once(settings, config_module.job_agent_home(), send=notifier(settings)), ensure_ascii=False))
        return 0
    if args.command == 'reconcile-51job':
        from service.job51_reconcile import run
        try:
            print(json.dumps(run(database=args.database), ensure_ascii=False))
            return 0
        except Exception as error:
            log.error('51job reconciliation failed error_type=%s', type(error).__name__)
            return 1
    if args.command == 'resume-platform':
        from service.safety import resume_platform
        try:
            result=resume_platform(args.platform)
            print(json.dumps(result,ensure_ascii=False))
            return 0 if result['resumed'] else 1
        except Exception as error:
            log.error('resume failed error_type=%s',type(error).__name__)
            return 1
    if args.command == 'resume-status':
        from service.resume_status import resume_status
        try:
            print(json.dumps(resume_status(database=args.database,before=args.before,include_jobs=args.include_jobs,limit=args.limit),ensure_ascii=False))
            return 0
        except Exception as error:
            log.error('resume status failed error_type=%s',type(error).__name__)
            return 1
    if args.command == 'review-resume-record':
        from pathlib import Path
        from service.original_application import review_resume_record
        try:
            evidence=json.loads(Path(args.record_file).expanduser().read_text(encoding='utf-8'))
            print(json.dumps(review_resume_record(args.job_id,evidence,database=args.database),ensure_ascii=False))
            return 0
        except Exception as error:
            log.error('resume record review failed error_type=%s',type(error).__name__)
            return 1
    if args.command == 'verify-receipt':
        from service.receipt_audit import verify_uncertain
        try:
            print(json.dumps(verify_uncertain(args.job_id),ensure_ascii=False))
            return 0
        except Exception as error:
            log.error('receipt audit failed error_type=%s',type(error).__name__)
            return 1
    if args.command == 'daily':
        from service.daily import daily_once
        try:
            result = daily_once(max_per_platform=args.max_per_platform, collection_page_limit=args.collection_page_limit)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get('status') in {'completed', None} else 1
        except KeyboardInterrupt:
            return 130
        except Exception as error:
            log.error('daily failed error_type=%s', type(error).__name__)
            return 1
    if args.command == 'collect':
        from collect.runtime import run
        return run(args.platform, dry_run=args.dry_run)
    if args.command == 'deliver':
        from store import SQLiteDeliveryStore
        from service.deliver import preview, confirm
        try:
            config = load_config()
            with SQLiteDeliveryStore(args.database, dry_run=config.engine.dry_run) as store:
                if args.preview:
                    if args.notify:
                        raise ValueError('Preview cannot notify')
                    print(preview(store, config, job_ids=args.job_ids))
                else:
                    if args.job_ids:
                        raise ValueError('Confirm uses only immutable batch jobs')
                    print(confirm(store, config, args.confirm, notify=args.notify))
            return 0
        except (ValueError, KeyError, RuntimeError, NotImplementedError) as error:
            log.error('deliver failed error_type=%s',type(error).__name__)
            return 1
    if args.command == 'monitor':
        from monitor.runtime import run
        return run(once=args.once)
    try:
        while True:
            try:
                from monitor.config import load_settings
                config, _ = load_settings()
                result = poll_once(config=config)
            except Exception as error:
                # YAML/provider exceptions may contain secrets; log type only.
                log.error('watch poll failed error_type=%s', type(error).__name__)
                if args.once:
                    return 1
                time.sleep(300)
                continue
            if args.once:
                print(json.dumps(result))
                return 1 if result['failed'] else 0
            log.info('watch poll completed sent=%s failed=%s skipped=%s',
                     result['sent'], result['failed'], result['skipped'])
            if not config.notify.enabled or not any(c.enabled for c in config.notify.channels.values()):
                log.info('notifications disabled; watch exited')
                return 0
            time.sleep(config.notify.interval_minutes * 60)
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
