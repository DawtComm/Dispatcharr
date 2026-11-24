"""
Optional "refresh profile info after sync" for Xtream Codes accounts.

By default the per-profile account-info logins (``refresh_account_profiles``)
are queued while the group refresh is still starting, so they run at the same
time as the live and VOD refreshes. For providers that throttle or block
bursts of logins from one IP, an account can set
``custom_properties["refresh_profiles_after_sync"]``. The profile logins then
run only after the live refresh and, when VOD is enabled, after the VOD
refresh has finished or failed.
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.m3u import tasks
from apps.m3u.models import M3UAccount
from apps.m3u.serializers import M3UAccountSerializer

FLAG = "refresh_profiles_after_sync"


def make_account(flag=None, account_type=M3UAccount.Types.XC, **extra):
    custom = dict(extra)
    if flag is not None:
        custom[FLAG] = flag
    return M3UAccount.objects.create(
        name="Provider",
        server_url="http://example.invalid",
        username="user",
        password="pass",
        account_type=account_type,
        is_active=True,
        custom_properties=custom,
    )


class FlagTests(TestCase):
    def test_flag_defaults_to_off(self):
        self.assertFalse(tasks._profile_refresh_after_sync_enabled(make_account()))

    def test_flag_on(self):
        self.assertTrue(
            tasks._profile_refresh_after_sync_enabled(make_account(flag=True))
        )

    def test_flag_off_when_explicitly_false(self):
        self.assertFalse(
            tasks._profile_refresh_after_sync_enabled(make_account(flag=False))
        )


class GroupRefreshQueueTests(TestCase):
    """The early queue site inside refresh_m3u_groups."""

    @patch("apps.m3u.tasks.refresh_account_profiles")
    def test_queues_during_group_refresh_by_default(self, profiles_task):
        account = make_account()
        tasks._queue_profile_refresh_with_group_refresh(account)
        profiles_task.delay.assert_called_once_with(account.id)

    @patch("apps.m3u.tasks.refresh_account_profiles")
    def test_does_not_queue_when_flag_on(self, profiles_task):
        account = make_account(flag=True)
        tasks._queue_profile_refresh_with_group_refresh(account)
        profiles_task.delay.assert_not_called()


class VodQueueTests(TestCase):
    @patch("apps.vod.tasks.refresh_vod_content")
    def test_plain_queue_when_not_chaining(self, vod_task):
        chained = tasks._queue_vod_refresh(42, chain_profiles=False)
        vod_task.delay.assert_called_once_with(42)
        vod_task.apply_async.assert_not_called()
        self.assertFalse(chained)

    @patch("apps.m3u.tasks.refresh_account_profiles")
    @patch("apps.vod.tasks.refresh_vod_content")
    def test_chain_runs_profiles_on_success_and_on_error(self, vod_task, profiles_task):
        sentinel = MagicMock(name="profiles_signature")
        profiles_task.si.return_value = sentinel

        chained = tasks._queue_vod_refresh(42, chain_profiles=True)

        profiles_task.si.assert_called_with(42)
        vod_task.apply_async.assert_called_once_with(
            args=(42,), link=sentinel, link_error=sentinel
        )
        vod_task.delay.assert_not_called()
        self.assertTrue(chained)

    @patch("apps.m3u.tasks.refresh_account_profiles")
    @patch("apps.vod.tasks.refresh_vod_content")
    def test_failure_to_queue_reports_not_chained(self, vod_task, profiles_task):
        vod_task.apply_async.side_effect = RuntimeError("broker down")
        self.assertFalse(tasks._queue_vod_refresh(42, chain_profiles=True))


@patch("apps.m3u.tasks._release_task_db_connection")
@patch("apps.m3u.tasks._ensure_m3u_refresh_terminal_status")
@patch("apps.m3u.tasks._set_m3u_account_status")
@patch("apps.m3u.tasks.release_task_lock")
@patch("apps.m3u.tasks.TaskLockRenewer")
@patch("apps.m3u.tasks.acquire_task_lock", return_value=True)
@patch("apps.m3u.tasks.refresh_account_profiles")
class WrapperQueuesProfilesTests(TestCase):
    """refresh_single_m3u_account queues the profile pass once everything else is done."""

    def run_wrapper(self, account, impl):
        with patch("apps.m3u.tasks._refresh_single_m3u_account_impl", side_effect=impl):
            tasks.refresh_single_m3u_account(account.id)

    def test_queued_after_a_refresh_with_no_vod_chain(self, profiles_task, *_mocks):
        account = make_account(flag=True)
        self.run_wrapper(account, lambda account_id, post_sync=None: "ok")
        profiles_task.delay.assert_called_once_with(account.id)

    def test_queued_even_when_the_refresh_fails(self, profiles_task, *_mocks):
        account = make_account(flag=True)

        def boom(account_id, post_sync=None):
            raise RuntimeError("provider unreachable")

        self.run_wrapper(account, boom)
        profiles_task.delay.assert_called_once_with(account.id)

    def test_not_queued_again_when_vod_already_chained_it(self, profiles_task, *_mocks):
        account = make_account(flag=True)

        def chained(account_id, post_sync=None):
            post_sync["profiles_chained"] = True
            return "ok"

        self.run_wrapper(account, chained)
        profiles_task.delay.assert_not_called()

    def test_not_queued_when_flag_off(self, profiles_task, *_mocks):
        account = make_account()
        self.run_wrapper(account, lambda account_id, post_sync=None: "ok")
        profiles_task.delay.assert_not_called()

    def test_not_queued_for_non_xc_accounts(self, profiles_task, *_mocks):
        account = make_account(flag=True, account_type=M3UAccount.Types.STADNARD)
        self.run_wrapper(account, lambda account_id, post_sync=None: "ok")
        profiles_task.delay.assert_not_called()


class SerializerTests(TestCase):
    def test_representation_defaults_to_false(self):
        data = M3UAccountSerializer(make_account()).data
        self.assertIs(data[FLAG], False)

    def test_representation_reflects_stored_value(self):
        data = M3UAccountSerializer(make_account(flag=True)).data
        self.assertIs(data[FLAG], True)

    def test_update_sets_flag_and_keeps_other_custom_properties(self):
        account = make_account(enable_vod=True)
        serializer = M3UAccountSerializer(account, data={FLAG: True}, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        account.refresh_from_db()
        self.assertIs(account.custom_properties[FLAG], True)
        self.assertIs(account.custom_properties["enable_vod"], True)

    def test_update_without_the_field_leaves_it_alone(self):
        account = make_account(flag=True)
        serializer = M3UAccountSerializer(account, data={"name": "Renamed"}, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        account.refresh_from_db()
        self.assertIs(account.custom_properties[FLAG], True)

    def test_update_can_turn_it_off(self):
        account = make_account(flag=True)
        serializer = M3UAccountSerializer(account, data={FLAG: False}, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        account.refresh_from_db()
        self.assertIs(account.custom_properties[FLAG], False)

    def test_create_persists_flag_and_defaults_to_off(self):
        base = {
            "name": "New provider",
            "server_url": "http://example.invalid",
            "username": "user",
            "password": "pass",
            "account_type": M3UAccount.Types.XC,
        }
        on = M3UAccountSerializer(data={**base, FLAG: True})
        self.assertTrue(on.is_valid(), on.errors)
        self.assertIs(on.save().custom_properties[FLAG], True)

        off = M3UAccountSerializer(data={**base, "name": "Other provider"})
        self.assertTrue(off.is_valid(), off.errors)
        self.assertIs(off.save().custom_properties[FLAG], False)
