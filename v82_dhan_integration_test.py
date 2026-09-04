import os,sys
sys.path.insert(0,os.path.dirname(__file__))
from v82_dhan_gateway import DhanV82Gateway

class Resp:
    def __init__(self,status,data): self.status_code=status; self._d=data; self.text=str(data)
    def json(self): return self._d
class FakeSession:
    def __init__(self): self.calls=[]; self.order_count=0; self.headers={}
    def post(self,url,json=None,timeout=12):
        self.calls.append(('POST',url,json))
        if url.endswith('/margincalculator'): return Resp(200,{'totalMargin':1000,'availableBalance':10000,'leverage':'4.5','insufficientBalance':0})
        if url.endswith('/orders'):
            self.order_count+=1; return Resp(200,{'orderId':f'O{self.order_count}','orderStatus':'PENDING'})
        if url.endswith('/marketfeed/ltp'): return Resp(200,{'data':{'NSE_EQ':{'123':{'last_price':101.2}}},'status':'success'})
        if url.endswith('/marketfeed/ohlc'): return Resp(200,{'data':{'NSE_EQ':{'123':{'last_price':101.2,'ohlc':{'open':100,'close':99,'high':102,'low':98}}}},'status':'success'})
        return Resp(200,{})
    def get(self,url,timeout=12):
        self.calls.append(('GET',url,None))
        if '/orders/O1' in url:return Resp(200,{'orderId':'O1','orderStatus':'TRADED','filledQty':10,'remainingQuantity':0,'averageTradedPrice':101.1})
        if '/orders/O2' in url:return Resp(200,{'orderId':'O2','orderStatus':'PART_TRADED','filledQty':4,'remainingQuantity':6,'averageTradedPrice':101.0})
        if url.endswith('/orders'):return Resp(200,[])
        if url.endswith('/positions'):return Resp(200,[{'securityId':'123','netQty':10}])
        if url.endswith('/fundlimit'):return Resp(200,{'availableBalance':10000})
        if url.endswith('/profile'):return Resp(200,{'dhanClientId':'C1','tokenValidity':'OK'})
        if url.endswith('/ip/getIP'):return Resp(200,{'primaryIP':'1.2.3.4'})
        return Resp(200,{})
    def delete(self,url,timeout=12):
        self.calls.append(('DELETE',url,None)); return Resp(202,{'orderId':'O1','orderStatus':'CANCELLED'})

def test():
    s=FakeSession(); g=DhanV82Gateway('C1','T',session=s)
    assert g.market_ltp(['123'])['123']==101.2
    assert g.market_ohlc(['123'])['123']['open']==100
    m=g.calculate_margin('123',10,'BUY',101.2); assert m['leverage']=='4.5'
    o=g.place_order('123',10,0,'BUY','MARKET'); assert o['orderStatus']=='PENDING'
    f=g.verify_fill('O1'); assert f['status']=='FILLED' and f['qty']==10 and f['price']==101.1
    f2=g.verify_fill('O2'); assert f2['status']=='PARTIAL' and f2['qty']==4 and f2['remaining']==6
    assert g.verify_position('123','LONG')==10
    c=g.cancel_order('O1'); assert c['orderStatus']=='CANCELLED'
    assert g.preflight(enforce_static_ip=True)[0]
    print('DHAN V2 MOCK INTEGRATION PASSED: actual response envelopes -> LTP/OHLC -> margin -> order -> full/partial fill -> position -> 202 cancel -> preflight')
if __name__=='__main__':test()
