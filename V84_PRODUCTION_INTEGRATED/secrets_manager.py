import boto3
from botocore.exceptions import ClientError

_ssm = boto3.client('ssm', region_name='ap-south-1')
_cache = {}

def get_parameter(name, decrypt=True):
    if name in _cache:
        return _cache[name]
    try:
        resp = _ssm.get_parameter(Name=name, WithDecryption=decrypt)
        value = resp['Parameter']['Value']
        _cache[name] = value
        return value
    except ClientError as e:
        print(f"[SSM ERROR] Failed to fetch {name}: {e}")
        return None

def get_dhan_token():
    return get_parameter('/trading-engine/dhan/access-token')

def get_dhan_client_id():
    return get_parameter('/trading-engine/dhan/client-id')

def get_gemini_api_key():
    return get_parameter('/trading-engine/ai/gemini-api-key')

def get_grok_api_key():
    return get_parameter('/trading-engine/ai/grok-api-key')

def get_groq_api_key():
    return get_parameter('/trading-engine/ai/groq-api-key')

def get_ses_sender():
    return get_parameter('/trading-engine/ses/sender-email')

def get_ses_recipient():
    return get_parameter('/trading-engine/ses/recipient-email')

def clear_cache():
    _cache.clear()
