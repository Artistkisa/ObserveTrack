import json
import time
from unittest.mock import patch

from test_observetrack import DatabaseCase
from observetrack import db, github
from observetrack.app import detail


class BoundaryTests(DatabaseCase):
    def test_restart_resumes_read_only_collection_with_same_identity(self):
        job = self.job()
        db.claim()
        db.recover()
        self.assertEqual(db.claim()['id'], job['id'])

    def test_baseline_undated_tags_are_not_new_activity(self):
        job = self.job()
        item = github.normalize({'name': 'v1.0', 'commit': {'sha': 'abc'}}, 'tags')
        self.assertEqual(github.store_items(self.repo, job['id'], 'tags', {'v1.0': item}, True, time.time() - 7 * 86400), 0)

    def test_equal_timestamp_pagination_never_loses_changes(self):
        job = self.job()
        items = {}
        for number in range(7):
            item = self.item(str(number))
            item['entity_id'] = str(number)
            items[str(number)] = item
        github.store_items(self.repo, job['id'], 'issues', items, False, 0)
        cursor, identities = '', []
        while True:
            page = detail(self.repo, before=cursor, limit=2, auth={})
            identities.extend(item['id'] for item in page['events'])
            if not page['has_more']:
                break
            cursor = page['next_before']
        self.assertEqual(len(identities), 7)
        self.assertEqual(len(set(identities)), 7)

    def test_community_responses_use_actual_issue_association(self):
        from observetrack.reports import community_metrics
        issue = self.item()
        comment = github.normalize({'id': 2, 'body': 'reply', 'created_at': self.period + 'T03:00:00Z',
                                    'issue_url': 'https://api.github.com/repos/test/repository/issues/1',
                                    'user': {'login': 'bob'}}, 'issue_comments')
        result = community_metrics([{'dimension': 'issues', **issue}, {'dimension': 'issue_comments', **comment}])
        self.assertEqual(result['first_response_hours'], 2)
