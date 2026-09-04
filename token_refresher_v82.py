#!/usr/bin/env python3
"""Dhan V2 TOTP token refresh using the documented HTTP endpoint.
Credentials remain in AWS SSM; the generated 24h token is written back to SSM.
"""
import boto3, pyotp, requests, logging
logging.basicConfig(level=logging.INFO,format='%(asctime)s [%(levelname)s] %(message)s')
log=logging.getLogger('V82Token')
ssm=boto3.client('ssm',region_name='ap-south-1')

def param(name): return ssm.get_parameter(Name=name,WithDecryption=True)['Parameter']['Value']
def put_token(token): ssm.put_parameter(Name='/trading-engine/dhan/access-token',Value=token,Type='SecureString',Overwrite=True)

def refresh():
    client=param('/trading-engine/dhan/client-id')
    pin=param('/trading-engine/dhan/pin') if _exists('/trading-engine/dhan/pin') else param('/trading-engine/dhan/password')
    secret=param('/trading-engine/dhan/totp-secret')
    otp=pyotp.TOTP(secret).now()
    r=requests.post('https://auth.dhan.co/app/generateAccessToken',params={'dhanClientId':client,'pin':pin,'totp':otp},timeout=15)
    r.raise_for_status(); data=r.json(); token=data.get('accessToken') or (data.get('data') or {}).get('accessToken')
    if not token: raise RuntimeError('Dhan token missing in response')
    put_token(token); log.info('Dhan access token refreshed successfully; expiry=%s',data.get('expiryTime')); return True

def _exists(name):
    try:ssm.get_parameter(Name=name,WithDecryption=True); return True
    except Exception:return False

if __name__=='__main__': refresh()
