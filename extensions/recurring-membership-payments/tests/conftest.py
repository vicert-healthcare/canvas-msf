"""Shared pytest fixtures for recurring_membership_payments.

Creates the SQLite tables for this plugin's own custom models before any
test runs. Nothing else does it. pytest-canvas, brought in by the
canvas[test-utils] dependency, is nineteen lines that import canvas_sdk and
wrap each test in a transaction, and it creates no table for a custom model,
so without the fixture below every custom model query fails with no such
table. The tables are created here the way the installer creates them, by
driving plugin_runner.ddl directly for this plugin's real package name,
recurring_membership_payments, which carries the client prefix rather
than matching the container directory.
"""

import importlib
from pathlib import Path

import pytest


_PLUGIN_NAME = "recurring_membership_payments"
_MODEL_MODULES = (
    "recurring_membership_payments.models.proxy",
    "recurring_membership_payments.models.membership",
    "recurring_membership_payments.models.membership_charge",
)

# Every handler module that calls render_to_string, so a rendered assertion
# reads the real templates/*.html files rather than a stand in for them.
_TEMPLATE_RENDERING_MODULES = (
    "recurring_membership_payments.handlers.portal_api",
    "recurring_membership_payments.handlers.chart_api",
    "recurring_membership_payments.handlers.members_api",
)

# The project root, the directory carrying pyproject.toml and this plugin's
# own package, recurring_membership_payments/. render_to_string resolves
# a plugin's template directory as PLUGIN_DIRECTORY / plugin_name, and outside
# an installed plugin PLUGIN_DIRECTORY has no reason to already point here.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _render_to_string_finds_this_plugin(monkeypatch):
    """Let render_to_string resolve this project's own templates during a test.

    render_to_string is decorated with canvas_sdk.utils.plugins.plugin_context,
    which walks the call stack for a frame whose globals carry __is_plugin__
    and then reads the plugin's own template directory off the module level
    PLUGIN_DIRECTORY constant that module already imported by reference. A
    handler instantiated directly by a test, rather than loaded through the
    plugin Sandbox, carries neither, so both are supplied here, once, for
    every handler module that renders a template, rather than repeated in
    every test that exercises one.
    """
    import canvas_sdk.utils.plugins as plugins_module

    monkeypatch.setattr(plugins_module, "PLUGIN_DIRECTORY", str(_PROJECT_ROOT))
    for module_name in _TEMPLATE_RENDERING_MODULES:
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "__is_plugin__", True, raising=False)


@pytest.fixture(scope="session", autouse=True)
def _create_membership_tables(django_db_setup, django_db_blocker):
    """Create the membership and membershipcharge tables in the SQLite test database.

    Drives the same plugin_runner.ddl functions the installer uses, importing
    the model modules directly so the registered classes are the same objects
    the tests import, rather than re-executing them through the plugin
    Sandbox.
    """
    from django.conf import settings

    if "sqlite3" not in settings.DATABASES["default"]["ENGINE"]:
        return

    for module_name in _MODEL_MODULES:
        importlib.import_module(module_name)

    from django.apps import apps

    from plugin_runner.ddl import (
        execute_create_table_sql,
        generate_create_table_sql,
        should_create_table,
    )
    from plugin_runner.installation import register_plugin_app_config

    with django_db_blocker.unblock():
        plugin_models = apps.all_models.get(_PLUGIN_NAME, {})
        for model_class in plugin_models.values():
            if should_create_table(model_class, _PLUGIN_NAME):
                create_sql = generate_create_table_sql(_PLUGIN_NAME, model_class)
                execute_create_table_sql(create_sql)

        register_plugin_app_config(_PLUGIN_NAME)
