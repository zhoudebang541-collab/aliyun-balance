import json
import os
import sys
import tempfile
import unittest

from aliyun_balance.store import AccountStore, StoreError, unprotect_secret


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "accounts.json")
        self.store = AccountStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_public_shape_hides_secret(self):
        result = self.store.upsert_many([
            {"name": "学生号", "ak": "LTAI5tTest12345678", "sk": "Secret1234567890abcdef"},
        ])
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["errors"], [])
        public = self.store.public_list()[0]
        self.assertEqual(public["name"], "学生号")
        self.assertTrue(public["hasSk"])
        self.assertNotIn("sk", public)
        self.assertEqual(public["akMasked"], "LTAI****5678")
        self.assertEqual(self.store.credentials(public["id"])["sk"], "Secret1234567890abcdef")

    def test_secret_is_not_plaintext_on_disk(self):
        self.store.upsert_many([{"ak": "LTAI5tTest12345678", "sk": "SuperSecretValue123"}])
        with open(self.path, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("SuperSecretValue123", raw)
        token = self.store.get(self.store.public_list()[0]["id"])["sk"]
        self.assertTrue(token.startswith("dpapi:v1:") or token.startswith("b64:v1:"))
        self.assertEqual(unprotect_secret(token), "SuperSecretValue123")

    def test_duplicate_ak_updates_instead_of_adding(self):
        self.store.upsert_many([{"name": "A", "ak": "LTAI5tSameKey123456", "sk": "Secret1234567890"}])
        result = self.store.upsert_many([{"name": "B", "ak": "LTAI5tSameKey123456", "sk": "Secret9999999999"}])
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(len(self.store.public_list()), 1)
        account_id = self.store.public_list()[0]["id"]
        self.assertEqual(self.store.credentials(account_id)["sk"], "Secret9999999999")

    def test_update_blank_secret_keeps_old(self):
        self.store.upsert_many([{"ak": "LTAI5tTest12345678", "sk": "Secret1234567890"}])
        account_id = self.store.public_list()[0]["id"]
        self.store.update(account_id, {"name": "新名字", "sk": ""})
        self.assertEqual(self.store.public_list()[0]["name"], "新名字")
        self.assertEqual(self.store.credentials(account_id)["sk"], "Secret1234567890")

    def test_invalid_endpoint_is_row_error(self):
        result = self.store.upsert_many([
            {"ak": "LTAI5tTest12345678", "sk": "Secret1234567890", "endpoint": "evil.example.com"},
        ])
        self.assertEqual(result["added"], 0)
        self.assertEqual(len(result["errors"]), 1)

    def test_delete_and_clear(self):
        self.store.upsert_many([{"ak": "LTAI5tTest12345678", "sk": "Secret1234567890"}])
        account_id = self.store.public_list()[0]["id"]
        self.assertTrue(self.store.delete(account_id))
        self.assertFalse(self.store.delete(account_id))
        self.assertEqual(self.store.count(), 0)
        self.store.upsert_many([{"ak": "LTAI5tOther123456789", "sk": "Secret1234567890"}])
        self.assertEqual(self.store.clear(), 1)

    def test_corrupt_file_is_backed_up(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        store = AccountStore(self.path)
        self.assertEqual(store.count(), 0)
        backups = [n for n in os.listdir(self.tmp.name) if ".corrupt-" in n]
        self.assertEqual(len(backups), 1)

    def test_missing_account_raises(self):
        with self.assertRaises(StoreError):
            self.store.credentials("aaaaaaaaaaaa")


if __name__ == "__main__":
    unittest.main()
