import shutil
import subprocess
import unittest
from pathlib import Path


class SkillsProjectFilterTest(unittest.TestCase):
    def setUp(self):
        self.page = (Path(__file__).resolve().parents[1] / 'web/templates/skills.html').read_text(encoding='utf-8')

    def test_project_selector_reuses_shared_preferences_and_filters_tabs_and_search(self):
        self.assertIn('id="filter-project" aria-label="筛选项目"', self.page)
        self.assertIn('onchange="onSkillProjectChange()"', self.page)
        self.assertIn('initProjectIdFilter(select, allProjects, renderSkills)', self.page)
        self.assertIn('const sorted = filterSkillsForProject(filtered)', self.page)
        self.assertIn('if (filterSkillsForProject(results).length === 0)', self.page)
        self.assertIn('const matchingResults = filterSkillsForProject(semanticResults)', self.page)
        self.assertIn('initializeSkillMarket();', self.page)

    def test_project_filter_preserves_global_and_module_only_skills(self):
        if not shutil.which('node'):
            self.skipTest('Node.js required for frontend rendering test')
        function = self.page.split('function filterSkillsForProject', 1)[1].split('function onSkillProjectChange', 1)[0]
        script = 'function filterSkillsForProject' + function + '''
const assert = require('node:assert/strict');
const items = [
    {id:1,scope:'global',applicable_projects:[9]},
    {id:2,scope:'scoped',applicable_projects:[6]},
    {id:3,scope:'scoped',applicable_projects:['6',9]},
    {id:4,scope:'scoped',applicable_projects:[9]},
    {id:5,scope:'scoped',applicable_projects:[],applicable_modules:['other']},
    {id:6,scope:'admin'},
];
assert.deepEqual(filterSkillsForProject(items,'6').map(s=>s.id), [1,2,3,5,6]);
assert.deepEqual(filterSkillsForProject(items,9).map(s=>s.id), [1,3,4,5,6]);
assert.equal(filterSkillsForProject(items,''), items);
assert.deepEqual(filterSkillsForProject([],6), []);
let selected = '6';
global.$ = () => ({value:selected});
assert.deepEqual(filterSkillsForProject(items).map(s=>s.id), [1,2,3,5,6]);
selected = '9';
assert.deepEqual(filterSkillsForProject(items).map(s=>s.id), [1,3,4,5,6]);
assert.equal(items.length,6);
'''
        subprocess.run(['node', '-e', script], check=True, capture_output=True)
