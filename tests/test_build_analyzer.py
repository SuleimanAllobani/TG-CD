import unittest
from ai.diagnostic_analyzer import analyze_job


def job(msg):
    return {'job_id': 'j', 'agent_id': 'a', 'site_name': 'S', 'status': 'failed', 'result_summary': msg,
            'result': {'message': msg, 'source': {'type': 'github', 'repo': 'acme/app', 'requested_ref': 'main'}}}


class BuildAnalyzerTests(unittest.TestCase):
    def check(self, msg, expected_text):
        a = analyze_job(job(msg), [])
        self.assertEqual(a['stage'], 'Build (GitHub source)')
        self.assertIn(expected_text.lower(), str(a).lower(), a)
        return a

    def test_dotnet_sdk_missing(self):
        self.check('GitHub source: Build failed: the .NET SDK is not installed on the agent machine (dotnet.exe not found).', '.NET SDK was not found')

    def test_npm_missing(self):
        self.check('GitHub source: Build failed: Node.js/npm is not installed on the agent machine (npm not found).', 'Node.js / npm was not found')

    def test_allowlist(self):
        self.check('GitHub source: Builds run code from the repository, so they only run for repositories listed in git.allowed_repos on the agent (acme/app is not listed).', 'allowed_repos')

    def test_timeout_is_not_reported_as_github_network_problem(self):
        a = self.check('GitHub source: Build failed: npm run build timed out after 900s and was stopped.', 'build timeout')
        self.assertNotIn('network problem', str(a).lower())

    def test_exit_code(self):
        self.check('GitHub source: Build failed (npm run build, exit code 1). Error: boom', 'Build command failed')

    def test_several_projects(self):
        self.check('GitHub source: Build failed: several .NET projects were found (a.csproj, b.csproj).', 'several web projects')


if __name__ == '__main__':
    unittest.main()
