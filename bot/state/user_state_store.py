from pathlib import Path
from common.json_store import JsonStore
class UserStateStore:
    def __init__(self,path='data/user_state.json'): self.store=JsonStore(path)
    def get(self,user_id): return self.store.read().get(str(user_id),{'state':'IDLE'})
    def set(self,user_id,state):
        d=self.store.read(); d[str(user_id)]=state; self.store.write(d)
    def update(self,user_id,**kw):
        s=self.get(user_id); s.update(kw); self.set(user_id,s); return s
    def clear(self,user_id):
        d=self.store.read(); d.pop(str(user_id),None); self.store.write(d)
