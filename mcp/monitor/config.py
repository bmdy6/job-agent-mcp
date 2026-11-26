"""Validate only monitor-owned settings in the shared configuration.

Collection/delivery may deploy a newer schema independently. Their settings must
neither block a read-only monitor nor be rewritten by its installer.
"""
import yaml
from config import Settings, job_agent_home
from engine.monitor_config import MonitorSettings


def load_settings():
    path = job_agent_home() / 'config.yaml'
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else {}
    except yaml.YAMLError:
        raise ValueError('Invalid JOB_AGENT_HOME/config.yaml YAML') from None
    if data is None:
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get('engine', {}), dict):
        raise ValueError('config.yaml and engine must be mappings')
    # Still validate all monitor/browser/notification keys strictly, including
    # dedicated-space constraints. Do not ignore typos in settings we consume.
    selected = {key: data[key] for key in ('notify', 'browser', 'scoring') if key in data}
    selected['engine'] = {key: data['engine'][key] for key in ('monitor', 'task_space_name')
                          if key in data.get('engine', {})}
    collect=data.get('collect',{})
    if isinstance(collect,dict) and 'task_space_name' in collect:
        selected['collect']={'task_space_name':collect['task_space_name']}
    platforms=data.get('engine',{}).get('platforms',{})
    if isinstance(platforms,dict):
        selected['engine']['platforms']={name:{k:v for k,v in options.items() if k in {'rate','daily_limit','interval_seconds'}}
                                       for name,options in platforms.items() if isinstance(options,dict)}
    config = Settings.model_validate(selected)
    return config, config.engine.monitor
