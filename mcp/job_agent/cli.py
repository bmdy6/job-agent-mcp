"""job-agent CLI for owned services and saved-reply notifications."""
from job_agent.observability import observed, report_error
import argparse
import json
import logging
import time

from job_agent.config import load_config
from job_agent.watch import poll_once

log = logging.getLogger(__name__)


def _setup_logging(command):
    """Rotate INFO logs under $JOB_AGENT_HOME/logs; fall back to stderr when config is unusable."""
    from job_agent.logsetup import setup_logging
    try:
        from job_agent.config import load_config, job_agent_home
        obs = load_config().observability
        setup_logging(command, home=job_agent_home(), max_bytes=obs.log_max_bytes, backups=obs.log_backups)
    except Exception:
        setup_logging(command)


@observed('cli')
def main(argv=None):
    parser = argparse.ArgumentParser(prog='job-agent')
    sub = parser.add_subparsers(dest='command', required=True)
    config_parser = sub.add_parser('settings', aliases=['config'], help='Show or preview/confirm model-validated settings')
    config_sub = config_parser.add_subparsers(dest='config_action', required=True)
    config_show = config_sub.add_parser('show')
    config_show.add_argument('section', nargs='?')
    config_set = config_sub.add_parser('set')
    config_set.add_argument('pairs', nargs='+', metavar='PATH JSON')
    config_set.add_argument('--confirm', action='store_true')
    config_sub.add_parser('history', help='Read the private settings audit and available rollback IDs')
    config_rollback = config_sub.add_parser('rollback', help='Preview/confirm exact pre-change file restoration')
    config_rollback.add_argument('id')
    config_rollback.add_argument('--confirm', action='store_true')
    for command in (config_set, config_rollback):
        command.add_argument('--actor', help='Operator identity; defaults to the OS user')
        command.add_argument('--reason', help='Single-line change reason; keep private messages and secrets out')
    sync = sub.add_parser('sync-default-greeting', help='Preview account greeting; --confirm writes and reads back, never sends HR messages')
    sync.add_argument('--platform', required=True, choices=('zhilian','51job'))
    sync.add_argument('--confirm', action='store_true')
    resume = sub.add_parser('resume-platform', help='Clear a risk pause after the user resolves the original site; never sends')
    resume.add_argument('platform',choices=('boss','zhilian','liepin','51job','nowcoder', 'maimai'))
    collect = sub.add_parser('collect', help='Collect read-only using JobSource and a dedicated Ego Lite space')
    collect.add_argument('--once', action='store_true', required=True, help='Run one resumable collection pass')
    collect.add_argument('--platform', action='append', choices=('boss', 'zhilian', 'liepin', '51job', 'nowcoder', 'maimai'))
    collect.add_argument('--dry-run', action='store_true', help='Fake sources and a separate SQLite database; no browser or notifications')
    daily = sub.add_parser('daily', help='Run an explicitly authorized daily collection/delivery round')
    daily.add_argument('--scheduled', action='store_true', help='Wait for active hours under daily lock')
    daily.add_argument('--once', action='store_true', required=True)
    daily.add_argument('--collection-page-limit', type=int, choices=range(1,21), help='Temporary lower collection page ceiling for acceptance')
    daily.add_argument('--max-per-platform', type=int, help='Acceptance-only cap, 1..3 jobs per platform')
    audit = sub.add_parser('verify-receipt', help='Read the original site for one uncertain application; never submits or rewrites history')
    audit.add_argument('--job-id', type=int, required=True, help='Owned database row ID of the uncertain attempt')
    reconcile = sub.add_parser('reconcile', help='Read-only original-site reconciliation; writes a review artifact only')
    reconcile.add_argument('--status', default='unverified,error')
    reconcile.add_argument('--platform', action='append', choices=('boss','zhilian','liepin','51job','nowcoder','maimai'))
    reconcile.add_argument('--database', help='Existing schema-v7 database; always opened mode=ro')
    reconcile.add_argument('--output', required=True, help='New private JSON artifact outside production home')
    reconcile.add_argument('--retry-run', help='Recheck only latest still_unknown outcomes from this reconciliation run')
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
    commands = sub.add_parser('command', help='私有求职指令；只读或修改数据库运行模式')
    commands.add_argument('action', choices=('status','today','pause','resume','only_reply'))
    commands.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'command':
        from job_agent.service.commands import command
        try:
            result = command(args.action)
            print(json.dumps(result, ensure_ascii=False) if args.json else result['message'])
            return 0
        except Exception:
            import sys
            print('求职指令执行失败，请在本地检查配置和数据库。', file=sys.stderr)
            return 1
    if args.command in {'settings', 'config'}:
        from job_agent.service.settings import get_settings, update_settings, settings_history, rollback_settings
        try:
            if args.config_action == 'show':
                result = get_settings(args.section)
            elif args.config_action == 'history':
                result = settings_history()
            elif args.config_action == 'rollback':
                result = rollback_settings(args.id, confirm=args.confirm, actor=args.actor, reason=args.reason, source='cli')
            else:
                if len(args.pairs) % 2:
                    raise ValueError('Supply PATH JSON pairs')
                changes = {}
                for path, raw in zip(args.pairs[::2], args.pairs[1::2]):
                    if path in changes:
                        raise ValueError('Duplicate settings path')
                    try:
                        changes[path] = json.loads(raw)
                    except ValueError:
                        raise ValueError('Settings values must be JSON; quote strings as JSON strings') from None
                result = update_settings(changes, confirm=args.confirm, actor=args.actor, reason=args.reason, source='cli')
            print(json.dumps(result, ensure_ascii=False))
            return 0
        except (ValueError, RuntimeError, OSError) as error:
            import sys
            # Service errors contain no raw settings values; I/O errors expose type only.
            message = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
            print(json.dumps({'error': message}, ensure_ascii=False), file=sys.stderr)
            return 1
    if args.command == 'dashboard':
        from job_agent.service.dashboard import generate
        try:
            print(json.dumps({'path': generate(args.out)}, ensure_ascii=False))
            return 0
        except Exception as error:
            # No log/config/runtime writes; errors expose type only.
            import sys
            print('dashboard failed: '+type(error).__name__, file=sys.stderr)
            return 1
    if args.command == 'sync-default-greeting':
        from job_agent.service.greeting_sync import sync_default_greeting
        print(json.dumps(sync_default_greeting(args.platform,args.confirm),ensure_ascii=False))
        return 0
    _setup_logging(args.command)
    if args.command == 'heartbeat':
        import job_agent.config as config_module
        from job_agent.service.heartbeat import run_once, notifier
        settings = config_module.load_config()
        print(json.dumps(run_once(settings, config_module.job_agent_home(), send=notifier(settings)), ensure_ascii=False))
        return 0
    if args.command == 'reconcile-51job':
        from job_agent.service.job51_reconcile import run
        try:
            print(json.dumps(run(database=args.database), ensure_ascii=False))
            return 0
        except Exception as error:
            report_error(error, stage=args.command)
            return 1
    if args.command == 'resume-platform':
        from job_agent.service.safety import resume_platform
        try:
            result=resume_platform(args.platform)
            print(json.dumps(result,ensure_ascii=False))
            return 0 if result['resumed'] else 1
        except Exception as error:
            report_error(error, stage=args.command)
            return 1
    if args.command == 'resume-status':
        from job_agent.service.resume_status import resume_status
        try:
            print(json.dumps(resume_status(database=args.database,before=args.before,include_jobs=args.include_jobs,limit=args.limit),ensure_ascii=False))
            return 0
        except Exception as error:
            report_error(error, stage=args.command)
            return 1
    if args.command == 'review-resume-record':
        from pathlib import Path
        from job_agent.service.original_application import review_resume_record
        try:
            evidence=json.loads(Path(args.record_file).expanduser().read_text(encoding='utf-8'))
            print(json.dumps(review_resume_record(args.job_id,evidence,database=args.database),ensure_ascii=False))
            return 0
        except Exception as error:
            report_error(error, stage=args.command)
            return 1
    if args.command == 'verify-receipt':
        from job_agent.service.receipt_audit import verify_uncertain
        try:
            print(json.dumps(verify_uncertain(args.job_id),ensure_ascii=False))
            return 0
        except Exception as error:
            report_error(error, stage=args.command)
            return 1
    if args.command == 'daily':
        from job_agent.service.daily import daily_once, DailyAlreadyRunning
        try:
            result = daily_once(max_per_platform=args.max_per_platform, collection_page_limit=args.collection_page_limit, scheduled=args.scheduled)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if (result.get('status') in {'completed', None}
                         or (result.get('status') == 'deferred' and result.get('reason_code', '').startswith('automation_'))) else 1
        except KeyboardInterrupt:
            return 130
        except DailyAlreadyRunning:
            # launchd fires every 30 minutes while a long round may still be pacing.
            log.info('daily skipped: another round is still running')
            print(json.dumps({'status': 'skipped', 'reason': 'already_running'}))
            return 0
        except Exception as error:
            report_error(error, stage=args.command)
            return 1
    if args.command == 'collect':
        from job_agent.collect.runtime import run
        return run(args.platform, dry_run=args.dry_run)
    if args.command == 'reconcile':
        from job_agent.service.reconcile import reconcile
        from job_agent.store.database import default_path
        print(json.dumps(reconcile(args.database or default_path(), args.output,
            statuses=args.status.split(','), platforms=args.platform, retry_run=args.retry_run), ensure_ascii=False))
        return 0
    if args.command == 'deliver':
        from job_agent.store import SQLiteDeliveryStore
        from job_agent.service.deliver import preview, confirm
        config = None
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
        except Exception as error:
            report_error(error, stage=args.command,alert=not (config is not None and config.engine.dry_run))
            return 1
    if args.command == 'monitor':
        from job_agent.monitor.runtime import run
        return run(once=args.once)
    try:
        while True:
            try:
                from job_agent.monitor.config import load_settings
                config, _ = load_settings()
                result = poll_once(config=config)
            except Exception as error:
                # YAML/provider exceptions may contain secrets; log type only.
                report_error(error, stage=args.command)
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
