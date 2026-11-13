import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / '.env', override=False)
DATA = Path(os.getenv('OBSERVE_DATA', 'data')).resolve()
DEFAULTS = {'timezone': 'Asia/Shanghai', 'collect_time': '07:30', 'daily_time': '08:00', 'weekly_time': '08:30',
            'retention_days': 365, 'daily_items': 20, 'weekly_items': 40, 'analysis_calls': 4, 'quiet': False,
            'analysis_v2_base_calls': 6, 'analysis_v2_extra_calls': 2, 'analysis_followup_requests': 12}

def analysis_v2_enabled(repo_id=None):
    enabled = os.getenv('OBSERVE_ANALYSIS_V2', 'false').lower() == 'true'
    canary = os.getenv('OBSERVE_ANALYSIS_V2_REPOSITORIES', '').strip()
    return enabled and (not canary or str(repo_id) in canary.split(','))


def readiness():
    return {'github': bool(os.getenv('OBSERVE_GITHUB_TOKEN')), 'model': all(os.getenv(key) for key in
            ['OBSERVE_MODEL_BASE_URL', 'OBSERVE_MODEL_NAME', 'OBSERVE_MODEL_API_KEY'])}
