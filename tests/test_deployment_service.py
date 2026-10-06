import tempfile, unittest, zipfile
from pathlib import Path
from agent.services.deployment_service import DeploymentService

def make_zip(path, text='ok'):
    with zipfile.ZipFile(path,'w') as z: z.writestr('index.html', f'<h1>{text}</h1>')
class DeploymentTests(unittest.TestCase):
    def test_first_failure_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            svc=DeploymentService(td); svc.health.check=lambda url:(False,'forced failure'); zp=Path(td)/'a.zip'; make_zip(zp)
            class F: filename='a.zip'; save=lambda self,p: Path(p).write_bytes(zp.read_bytes())
            pkg=svc.packages.store_file(F())
            r=svc.deploy({'request_id':'r1','requested_by_user_id':1,'agent_id':'local','project_type':'static','source_type':'uploaded_package','package_id':pkg['package_id'],'folder_path':None,'site_name':'s1','site_exists_expected':False,'create_site_if_missing':True,'site_port':8080,'site_host':None,'site_protocol':'http','app_pool_name':'p1','app_pool_exists_expected':False,'create_app_pool_if_missing':True,'app_pool_runtime':'','app_pool_pipeline_mode':'Integrated','health_check_url':'http://127.0.0.1:9/','notes':None})
            self.assertFalse(r['success']); self.assertTrue(r['first_deployment_failure']); self.assertTrue(r['failed_release_preserved'])
    def test_success_with_mocked_health(self):
        with tempfile.TemporaryDirectory() as td:
            svc=DeploymentService(td); svc.health.check=lambda url:(True,'HTTP 200')
            folder=Path(td)/'src'; folder.mkdir(); (folder/'index.html').write_text('ok')
            r=svc.deploy({'request_id':'r2','requested_by_user_id':1,'agent_id':'local','project_type':'static','source_type':'folder_path','package_id':None,'folder_path':str(folder),'site_name':'s2','site_exists_expected':False,'create_site_if_missing':True,'site_port':8080,'site_host':None,'site_protocol':'http','app_pool_name':'p2','app_pool_exists_expected':False,'create_app_pool_if_missing':True,'app_pool_runtime':'','app_pool_pipeline_mode':'Integrated','health_check_url':'http://example/','notes':None})
            self.assertTrue(r['success']); self.assertTrue((Path(td)/'current'/'s2'/'index.html').exists())
if __name__=='__main__': unittest.main()
