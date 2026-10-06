import unittest
from ai.diagnostic_analyzer import analyze_job, format_analysis, format_report


class Phase3AnalyzerTests(unittest.TestCase):
    def test_detects_404_health_failure_and_rollback(self):
        job = {
            'job_id': 'job_test',
            'agent_id': 'agent_test',
            'site_name': 'DemoSite',
            'status': 'failed',
            'health_check_url': 'http://127.0.0.1:8087/notfound',
            'result_summary': 'Health check failed: HTTP 404. Rollback completed successfully.',
            'result': {'rollback_completed': True},
        }
        logs = [{'message': 'HTTP 404 Not Found during health check. Rollback completed.'}]
        analysis = analyze_job(job, logs)
        text = format_analysis(analysis, job)
        self.assertEqual(analysis['stage'], 'Health check')
        self.assertIn('HTTP 404', '\n'.join(analysis['findings']))
        self.assertIn('Rollback completed', '\n'.join(analysis['findings']))
        self.assertIn('Deployment analysis', text)

    def test_report_contains_core_fields(self):
        job = {'job_id': 'job_test', 'status': 'success', 'agent_id': 'agent_test', 'site_name': 'DemoSite', 'app_pool_name': 'Pool', 'health_check_url': 'http://x', 'result_summary': 'OK'}
        text = format_report(job, [{'message': 'Job completed: success'}])
        self.assertIn('Deployment report', text)
        self.assertIn('Job: job_test', text)
        self.assertIn('Site: DemoSite', text)


if __name__ == '__main__':
    unittest.main()
