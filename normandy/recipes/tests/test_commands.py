import hashlib
import json
from unittest.mock import patch
from datetime import timedelta
import requests_mock

from django.conf import settings
from django.core.management import call_command, CommandError
from django.core.exceptions import ImproperlyConfigured

import pytest
import requests.exceptions
from markus.testing import MetricsMock
from markus import GAUGE

from normandy.base.tests import UserFactory, Whatever
from normandy.recipes import exports
from normandy.recipes.models import Action, Recipe
from normandy.recipes.tests import ActionFactory, RecipeFactory
from normandy.studies.tests import ExtensionFactory


@pytest.yield_fixture
def mock_action(settings, tmpdir):
    implementations = {}
    schemas = {}

    impl_patch = patch(
        "normandy.recipes.management.commands.update_actions.get_implementation",
        lambda name: implementations[name],
    )
    schema_by_implementation_patch = patch(
        "normandy.recipes.management.commands.update_actions"
        ".get_arguments_schema_by_implementation",
        lambda name, _: schemas[name],
    )
    schema_by_schemas_patch = patch(
        "normandy.recipes.management.commands.update_actions.get_arguments_schema_by_schemas",
        lambda name, _, _2: schemas[name],
    )

    # 'tmpdir' is a LocalPath object, turn it into a regular path string with str().
    settings.ACTIONS_ROOT_DIRECTORY = str(tmpdir)
    settings.ACTIONS_SCHEMA_DIRECTORY = str(tmpdir)

    schemas_json = tmpdir.join("schemas.json")
    # By default, make it an empty JSON file
    schemas_json.write(json.dumps({}))

    def _mock_action(name, schema, implementation=None):
        tmpdir.mkdir(name)
        if implementation:
            implementations[name] = implementation
        else:
            schemas_json.write(json.dumps({name: schema}))
        schemas[name] = schema

    with impl_patch, schema_by_implementation_patch, schema_by_schemas_patch:
        yield _mock_action


@pytest.mark.django_db
class TestUpdateActions(object):
    def test_it_works(self):
        """
        Verify that the update_actions command doesn't throw an error.
        """
        call_command("update_actions")

    def test_it_creates_new_actions(self, mock_action):
        mock_action("test-action", {"type": "int"}, 'console.log("foo");')

        call_command("update_actions")
        assert Action.objects.count() == 1

        action = Action.objects.all()[0]
        assert action.name == "test-action"
        assert action.implementation == 'console.log("foo");'
        assert action.arguments_schema == {"type": "int"}

    def test_it_updates_existing_actions(self, mock_action):
        action = ActionFactory(name="test-action", implementation="old_impl", arguments_schema={})
        mock_action(action.name, {"type": "int"}, "new_impl")

        call_command("update_actions")
        assert Action.objects.count() == 1

        action.refresh_from_db()
        assert action.implementation == "new_impl"
        assert action.arguments_schema == {"type": "int"}

    def test_it_creates_new_actions_without_implementation(self, mock_action):
        mock_action("test-action", {"type": "int"})

        call_command("update_actions")
        assert Action.objects.count() == 1

        action = Action.objects.all()[0]
        assert action.name == "test-action"
        assert action.implementation is None
        assert action.arguments_schema == {"type": "int"}

    def test_it_updates_existing_actions_without_implementation(self, mock_action):
        action = ActionFactory(name="test-action", implementation=None, arguments_schema={})
        mock_action(action.name, {"type": "int"})

        call_command("update_actions")
        assert Action.objects.count() == 1

        action.refresh_from_db()
        assert action.implementation is None
        assert action.arguments_schema == {"type": "int"}

    def test_it_updates_existing_drops_implementation(self, mock_action):
        action = ActionFactory(name="test-action", implementation="old_impl", arguments_schema={})
        mock_action(action.name, {"type": "int"})
        old_implementation = action.implementation
        old_implementation_hash = action.implementation_hash

        call_command("update_actions")
        assert Action.objects.count() == 1

        action.refresh_from_db()
        assert action.implementation == old_implementation
        assert action.implementation_hash == old_implementation_hash
        assert action.arguments_schema == {"type": "int"}

    def test_it_doesnt_disable_recipes(self, mock_action):
        action = ActionFactory(name="test-action", implementation="old")
        recipe = RecipeFactory(action=action, approver=UserFactory(), enabler=UserFactory())
        action = recipe.approved_revision.action
        mock_action(action.name, "impl", action.arguments_schema)

        call_command("update_actions")
        recipe.refresh_from_db()
        assert recipe.approved_revision.enabled

    def test_it_only_updates_given_actions(self, mock_action):
        update_action = ActionFactory(name="update-action", implementation="old")
        dont_update_action = ActionFactory(name="dont-update-action", implementation="old")

        mock_action(update_action.name, update_action.arguments_schema, "new")
        mock_action(dont_update_action.name, dont_update_action.arguments_schema, "new")

        call_command("update_actions", "update-action")
        update_action.refresh_from_db()
        assert update_action.implementation == "new"
        dont_update_action.refresh_from_db()
        assert dont_update_action.implementation == "old"

    def test_it_ignores_missing_actions(self, mock_action):
        dont_update_action = ActionFactory(name="dont-update-action", implementation="old")
        mock_action(dont_update_action.name, dont_update_action.arguments_schema, "new")

        with pytest.raises(CommandError):
            call_command("update_actions", "missing-action")


class TestUpdateSignatures(object):
    @pytest.mark.django_db
    def test_it_works(self, mocker):
        """
        Verify that the update_recipe_signatures command doesn't throw an error.
        """
        call_command("update_signatures")

    def test_it_calls_other_update_signature_commands(self, mocker):
        prefix = "normandy.recipes.management.commands"
        update_recipe_signatures = mocker.patch(f"{prefix}.update_recipe_signatures.Command")
        update_action_signatures = mocker.patch(f"{prefix}.update_action_signatures.Command")

        call_command("update_signatures")
        update_action_signatures.return_value.execute.assert_called_once()
        update_recipe_signatures.return_value.execute.assert_called_once()


@pytest.mark.django_db
class TestUpdateRecipeSignatures(object):
    def test_it_works(self):
        """
        Verify that the update_recipe_signatures command doesn't throw an error.
        """
        call_command("update_recipe_signatures")

    def test_it_signs_unsigned_enabled_recipes(self, mocked_autograph):
        r = RecipeFactory(approver=UserFactory(), enabler=UserFactory(), signed=False)
        assert r.signature is None
        call_command("update_recipe_signatures")
        r.refresh_from_db()
        assert r.signature is not None

    def test_it_signs_out_of_date_recipes(self, settings, mocked_autograph):
        r = RecipeFactory(approver=UserFactory(), enabler=UserFactory(), signed=True)
        r.signature.timestamp -= timedelta(seconds=settings.AUTOGRAPH_SIGNATURE_MAX_AGE * 2)
        r.signature.signature = "old signature"
        r.signature.save()
        call_command("update_recipe_signatures")
        r.refresh_from_db()
        assert r.signature.signature != "old signature"

    def test_it_unsigns_disabled_recipes(self, mocked_autograph):
        r = RecipeFactory(approver=UserFactory(), signed=True)
        call_command("update_recipe_signatures")
        r.refresh_from_db()
        assert r.signature is None

    def test_it_unsigns_out_of_date_disabled_recipes(self, settings, mocked_autograph):
        r = RecipeFactory(approver=UserFactory(), signed=True, enabled=False)
        r.signature.timestamp -= timedelta(seconds=settings.AUTOGRAPH_SIGNATURE_MAX_AGE * 2)
        r.signature.save()
        call_command("update_recipe_signatures")
        r.refresh_from_db()
        assert r.signature is None

    def test_it_resigns_signed_recipes_with_force(self, mocked_autograph):
        r = RecipeFactory(approver=UserFactory(), enabler=UserFactory(), signed=True)
        r.signature.signature = "old signature"
        r.signature.save()
        call_command("update_recipe_signatures", "--force")
        r.refresh_from_db()
        assert r.signature.signature != "old signature"

    def test_it_updates_remote_settings_if_enabled(self, mocker, mocked_autograph):
        mocked_remotesettings = mocker.patch(
            "normandy.recipes.management.commands.update_recipe_signatures.RemoteSettings"
        )
        for i in range(3):
            r = RecipeFactory(approver=UserFactory(), enabler=UserFactory(), signed=True)
            r.signature.signature = "old signature"
            r.signature.save()
        mocked_remotesettings.reset_mock()

        call_command("update_recipe_signatures", "--force")

        assert mocked_remotesettings.return_value.publish.call_count == 3
        assert mocked_remotesettings.return_value.approve_changes.call_count == 1

    def test_it_does_not_resign_up_to_date_recipes(self, settings, mocked_autograph):
        r = RecipeFactory(approver=UserFactory(), enabler=UserFactory(), signed=True)
        r.signature.signature = "original signature"
        r.signature.save()
        call_command("update_recipe_signatures")
        r.refresh_from_db()
        assert r.signature.signature == "original signature"

    def test_it_sends_metrics(self, settings, mocked_autograph):
        # 3 to sign
        RecipeFactory.create_batch(3, approver=UserFactory(), enabler=UserFactory(), signed=False)
        # and 1 to unsign
        RecipeFactory(approver=UserFactory(), signed=True, enabled=False)

        with MetricsMock() as mm:
            call_command("update_recipe_signatures")
            mm.print_records()
            assert mm.has_record(GAUGE, stat="normandy.signing.recipes.signed", value=3)
            assert mm.has_record(GAUGE, stat="normandy.signing.recipes.unsigned", value=1)

    def test_it_does_not_send_excessive_remote_settings_traffic(
        self, mocker, settings, mocked_autograph
    ):
        # 10 to update
        recipes = RecipeFactory.create_batch(
            10, approver=UserFactory(), enabler=UserFactory(), signed=False
        )
        assert all(not r.approved_revision.uses_only_baseline_capabilities() for r in recipes)

        # Set up a version of the Remote Settings helper with a mocked out client
        client_mock = None

        def rs_with_mocked_client():
            nonlocal client_mock
            assert client_mock is None
            rs = exports.RemoteSettings()
            client_mock = mocker.MagicMock()
            rs.client = client_mock
            return rs

        mocker.patch(
            "normandy.recipes.management.commands.update_recipe_signatures.RemoteSettings",
            side_effect=rs_with_mocked_client,
        )

        call_command("update_recipe_signatures")

        # Make sure that our mock was actually used
        assert client_mock

        # One signing request to the capabilities collection
        assert client_mock.patch_collection.mock_calls == [
            mocker.call(
                id=settings.REMOTE_SETTINGS_CAPABILITIES_COLLECTION_ID,
                data={"status": "to-sign"},
                bucket=settings.REMOTE_SETTINGS_WORKSPACE_BUCKET_ID,
            )
        ]

        # one publish to the capabilities collection per recipe
        expected_calls = []
        for recipe in recipes:
            expected_calls.append(
                mocker.call(
                    data=Whatever(lambda r: r["id"] == recipe.id, name=f"Recipe {recipe.id}"),
                    bucket=settings.REMOTE_SETTINGS_WORKSPACE_BUCKET_ID,
                    collection=settings.REMOTE_SETTINGS_CAPABILITIES_COLLECTION_ID,
                )
            )
        client_mock.update_record.has_calls(expected_calls, any_order=True)  # all expected calls
        assert client_mock.update_record.call_count == len(expected_calls)  # no extra calls


@pytest.mark.django_db
class TestUpdateActionSignatures(object):
    def test_it_works(self):
        """
        Verify that the update_action_signatures command doesn't throw an error.
        """
        call_command("update_action_signatures")

    def test_it_signs_unsigned_actions(self, mocked_autograph):
        a = ActionFactory(signed=False)
        call_command("update_action_signatures")
        a.refresh_from_db()
        assert a.signature is not None

    def test_it_signs_out_of_date_actions(self, settings, mocked_autograph):
        a = ActionFactory(signed=True)
        a.signature.timestamp -= timedelta(seconds=settings.AUTOGRAPH_SIGNATURE_MAX_AGE * 2)
        a.signature.signature = "old signature"
        a.signature.save()
        call_command("update_action_signatures")
        a.refresh_from_db()
        assert a.signature.signature != "old signature"

    def test_it_resigns_signed_actions_with_force(self, mocked_autograph):
        a = ActionFactory(signed=True)
        a.signature.signature = "old signature"
        a.signature.save()
        call_command("update_action_signatures", "--force")
        a.refresh_from_db()
        assert a.signature.signature != "old signature"

    def test_it_does_not_resign_up_to_date_actions(self, settings, mocked_autograph):
        a = ActionFactory(signed=True)
        a.signature.signature = "original signature"
        a.signature.save()
        call_command("update_action_signatures")
        a.refresh_from_db()
        assert a.signature.signature == "original signature"

    def test_it_sends_metrics(self, settings, mocked_autograph):
        ActionFactory.create_batch(3, signed=False)
        with MetricsMock() as mm:
            call_command("update_action_signatures")
            mm.print_records()
            assert mm.has_record(GAUGE, stat="normandy.signing.actions.signed", value=3)


addonUrl = "addonUrl"


@pytest.mark.django_db
class TestUpdateAddonUrls(object):
    def test_it_works(self, storage):
        extension = ExtensionFactory()
        fake_old_url = extension.xpi.url.replace("/media/", "/media-old/")
        action = ActionFactory(name="opt-out-study")
        recipe = RecipeFactory(action=action, arguments={addonUrl: fake_old_url})
        call_command("update_addon_urls")

        # For reasons that I don't understand, recipe.update_from_db() doesn't work here.
        recipe = Recipe.objects.get(id=recipe.id)
        assert recipe.latest_revision.arguments[addonUrl] == extension.xpi.url

    def test_signatures_are_updated(self, mocked_autograph, storage):
        extension = ExtensionFactory()
        fake_old_url = extension.xpi.url.replace("/media/", "/media-old/")
        action = ActionFactory(name="opt-out-study")
        recipe = RecipeFactory(
            action=action,
            arguments={addonUrl: fake_old_url},
            approver=UserFactory(),
            enabler=UserFactory(),
            signed=True,
        )
        # preconditions
        assert recipe.signature is not None
        assert recipe.signature.signature == hashlib.sha256(recipe.canonical_json()).hexdigest()
        signature_before = recipe.signature.signature

        call_command("update_addon_urls")
        recipe.refresh_from_db()

        assert recipe.signature is not None
        assert recipe.signature != signature_before
        assert recipe.signature.signature == hashlib.sha256(recipe.canonical_json()).hexdigest()

    def test_it_doesnt_update_other_actions(self):
        action = ActionFactory(name="some-other-action")
        recipe = RecipeFactory(
            action=action, arguments={addonUrl: "https://before.example.com/extensions/addon.xpi"}
        )
        call_command("update_addon_urls")
        # For reasons that I don't understand, recipe.update_from_db() doesn't work here.
        recipe = Recipe.objects.get(id=recipe.id)
        # Url should not be not updated
        assert (
            recipe.latest_revision.arguments[addonUrl]
            == "https://before.example.com/extensions/addon.xpi"
        )

    def test_it_works_for_multiple_extensions(self, storage):
        extension1 = ExtensionFactory(name="1.xpi")
        extension2 = ExtensionFactory(name="2.xpi")

        fake_old_url1 = extension1.xpi.url.replace("/media/", "/media-old/")
        fake_old_url2 = extension2.xpi.url.replace("/media/", "/media-old/")

        action = ActionFactory(name="opt-out-study")
        recipe1 = RecipeFactory(action=action, arguments={"name": "1", addonUrl: fake_old_url1})
        recipe2 = RecipeFactory(action=action, arguments={"name": "2", addonUrl: fake_old_url2})
        call_command("update_addon_urls")

        # For reasons that I don't understand, recipe.update_from_db() doesn't work here.
        recipe1 = Recipe.objects.get(id=recipe1.id)
        recipe2 = Recipe.objects.get(id=recipe2.id)

        assert recipe1.latest_revision.arguments[addonUrl] == extension1.xpi.url
        assert recipe2.latest_revision.arguments[addonUrl] == extension2.xpi.url


@pytest.mark.django_db
class TestSyncRemoteSettings(object):
    capabilities_workspace_collection_url = (
        f"/v1/buckets/{settings.REMOTE_SETTINGS_WORKSPACE_BUCKET_ID}/collections"
        f"/{settings.REMOTE_SETTINGS_CAPABILITIES_COLLECTION_ID}"
    )
    capabilities_published_records_url = (
        f"/v1/buckets/{settings.REMOTE_SETTINGS_PUBLISH_BUCKET_ID}/collections"
        f"/{settings.REMOTE_SETTINGS_CAPABILITIES_COLLECTION_ID}/records"
    )

    @pytest.mark.django_db
    def test_it_works(self, rs_settings, requestsmock):
        """
        Verify that the sync_remote_settings command doesn't throw an error.
        """
        requestsmock.get(self.capabilities_published_records_url, json={"data": []})
        call_command("sync_remote_settings")

    def test_it_fails_if_not_enabled(self):
        # We enabled Remote Settings without mocking server calls.
        with pytest.raises(ImproperlyConfigured):
            call_command("sync_remote_settings")

    def test_it_fails_if_server_not_reachable(self, rs_settings, requestsmock):
        requestsmock.get(
            self.capabilities_published_records_url, exc=requests.exceptions.ConnectionError
        )

        with pytest.raises(requests.exceptions.ConnectionError):
            call_command("sync_remote_settings")

    def test_it_does_nothing_on_dry_run(self, rs_settings, requestsmock, mocked_remotesettings):
        r1 = RecipeFactory(name="Test 1", enabler=UserFactory(), approver=UserFactory())
        requestsmock.get(
            self.capabilities_published_records_url, json={"data": [exports.recipe_as_record(r1)]}
        )

        call_command("sync_remote_settings", "--dry-run")

        assert not mocked_remotesettings.publish.called
        assert not mocked_remotesettings.unpublish.called

    def test_publishes_missing_recipes(self, rs_settings, requestsmock):
        # Some records will be created with PUT.
        requestsmock.put(requests_mock.ANY, json={})
        # A signature request will be sent.
        requestsmock.patch(self.capabilities_workspace_collection_url, json={})
        # Instantiate local recipes.
        r1 = RecipeFactory(name="Test 1", enabler=UserFactory(), approver=UserFactory())
        r2 = RecipeFactory(name="Test 2", enabler=UserFactory(), approver=UserFactory())

        # Mock the server responses.
        # `r2` should be on the server
        requestsmock.get(
            self.capabilities_published_records_url, json={"data": [exports.recipe_as_record(r1)]}
        )
        # It will be created.
        r2_capabilities_url = self.capabilities_workspace_collection_url + f"/records/{r2.id}"
        requestsmock.put(r2_capabilities_url, json={})

        # Ignore any requests before this point
        requestsmock._adapter.request_history = []

        call_command("sync_remote_settings")

        requests = requestsmock.request_history
        # First request should be to get the existing records
        assert requests[0].method == "GET"
        assert requests[0].url.endswith(self.capabilities_published_records_url)
        # The next should be to PUT the missing recipe2
        assert requests[1].method == "PUT"
        assert requests[1].url.endswith(r2_capabilities_url)
        # The final one should be to approve the changes
        assert requests[2].method == "PATCH"
        assert requests[2].url.endswith(self.capabilities_workspace_collection_url)
        # And there are no extra requests
        assert len(requests) == 3

    def test_republishes_outdated_recipes(self, rs_settings, requestsmock):
        # Some records will be created with PUT.
        requestsmock.put(requests_mock.ANY, json={})
        # A signature request will be sent.
        requestsmock.patch(self.capabilities_workspace_collection_url, json={})
        # Instantiate local recipes.
        r1 = RecipeFactory(name="Test 1", enabler=UserFactory(), approver=UserFactory())
        r2 = RecipeFactory(name="Test 2", enabler=UserFactory(), approver=UserFactory())

        # Mock the server responses.
        to_update = {**exports.recipe_as_record(r2), "name": "Outdated name"}
        requestsmock.get(
            self.capabilities_published_records_url,
            json={"data": [exports.recipe_as_record(r1), to_update]},
        )
        # It will be updated.
        r2_capabilities_url = self.capabilities_workspace_collection_url + f"/records/{r2.id}"
        requestsmock.put(r2_capabilities_url, json={})

        # Ignore any requests before this point
        requestsmock._adapter.request_history = []
        call_command("sync_remote_settings")

        requests = requestsmock.request_history
        # The first request should be to get the existing records
        assert requests[0].method == "GET"
        assert requests[0].url.endswith(self.capabilities_published_records_url)
        # The next one should be to PUT the outdated recipe2
        assert requests[1].method == "PUT"
        assert requests[1].url.endswith(r2_capabilities_url)
        # The final one should be to approve the changes
        assert requests[2].method == "PATCH"
        assert requests[2].url.endswith(self.capabilities_workspace_collection_url)
        # And there are no extra requests
        assert len(requests) == 3

    def test_unpublishes_extra_recipes(self, rs_settings, requestsmock):
        # Some records will be created with PUT.
        requestsmock.put(requests_mock.ANY, json={})
        # A signature request will be sent.
        requestsmock.patch(self.capabilities_workspace_collection_url, json={})
        # Instantiate local recipes.
        r1 = RecipeFactory(name="Test 1", enabler=UserFactory(), approver=UserFactory())
        r2 = RecipeFactory(name="Test 2", approver=UserFactory())
        # Mock the server responses.
        # `r2` should not be on the server (not enabled)
        requestsmock.get(
            self.capabilities_published_records_url,
            json={"data": [exports.recipe_as_record(r1), exports.recipe_as_record(r2)]},
        )
        # It will be deleted.
        r2_capabilities_url = self.capabilities_workspace_collection_url + f"/records/{r2.id}"
        requestsmock.delete(r2_capabilities_url, json={"data": ""})

        # Ignore any requests before this point
        requestsmock._adapter.request_history = []
        call_command("sync_remote_settings")

        requests = requestsmock.request_history
        # The first request should be to get the existing records
        assert requests[0].method == "GET"
        assert requests[0].url.endswith(self.capabilities_published_records_url)
        # The next one should be to PUT the outdated recipe2
        assert requests[1].method == "DELETE"
        assert requests[1].url.endswith(r2_capabilities_url)
        # The final one should be to approve the changes
        assert requests[2].method == "PATCH"
        assert requests[2].url.endswith(self.capabilities_workspace_collection_url)
        # And there are no extra requests
        assert len(requests) == 3
