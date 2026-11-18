"""Provision a folder-scoped Yandex AI Studio identity for a local Huf site.

Run with the bench Python from the bench root. The current yc profile must
manage service accounts and role bindings in the specified folder.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ACCOUNT = "dinext-huf-raven"
DESCRIPTION = "Dinext Raven assistant via Huf"
ROLE = "ai.languageModels.user"
SCOPE = "yc.ai.languageModels.execute"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud-id", required=True)
    parser.add_argument("--folder-id", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--model", default="yandexgpt/latest", help="Model path within this folder")
    parser.add_argument("--yc", default="yc", help="Path to the configured Yandex Cloud CLI")
    args = parser.parse_args()

    def yc(*command):
        run = subprocess.run([args.yc, *command, "--format", "json"], capture_output=True, timeout=90)
        if run.returncode:
            # Key creation responses and CLI diagnostics stay outside console logs.
            raise RuntimeError("Yandex Cloud CLI failed at " + " ".join(command[:3]) +
                               f" (exit {run.returncode}). Check the active profile and folder access.")
        return json.loads(run.stdout) if run.stdout.strip() else None

    import frappe
    from dinext_huf_raven.setup import configure_yandex

    frappe.init(site=args.site, sites_path=str(Path("sites").resolve()))
    try:
        frappe.connect()
        frappe.set_user("Administrator")
        if not frappe.db.exists("DocType", "Dinext AI Settings"):
            raise RuntimeError("Install Dinext Huf Raven before provisioning its provider")
        settings = frappe.get_single("Dinext AI Settings")
        if settings.yandex_folder_id and settings.yandex_folder_id != args.folder_id:
            raise RuntimeError("The installed site is already associated with a different Yandex folder")
        folder = yc("resource-manager", "folder", "get", "--id", args.folder_id)
        if folder["cloud_id"] != args.cloud_id or folder.get("status") != "ACTIVE":
            raise RuntimeError("The active folder must belong to the requested cloud")
        accounts = yc("iam", "service-account", "list", "--folder-id", args.folder_id) or []
        account = next((a for a in accounts if a["name"] == ACCOUNT), None)
        if account and account.get("description") != DESCRIPTION:
            raise RuntimeError("An unrelated service account already has this name")
        if not account:
            account = yc("iam", "service-account", "create", "--folder-id", args.folder_id,
                         "--name", ACCOUNT, "--description", DESCRIPTION)
        account_id = account["id"]
        if settings.yandex_account_id and settings.yandex_account_id != account_id:
            raise RuntimeError("The local provider already belongs to a different service account")
        bindings = yc("resource-manager", "folder", "list-access-bindings", "--id", args.folder_id) or []
        if not any(b["role_id"] == ROLE and b["subject"]["id"] == account_id for b in bindings):
            yc("resource-manager", "folder", "add-access-binding", "--id", args.folder_id,
               "--role", ROLE, "--service-account-id", account_id)
        keys = yc("iam", "api-key", "list", "--service-account-id", account_id) or []
        stored = (frappe.db.exists("AI Provider", "DinextYandexAI")
                  and frappe.get_doc("AI Provider", "DinextYandexAI").get_password("api_key", raise_exception=False))
        registered = any(k["id"] == settings.yandex_key_id for k in keys)
        if settings.yandex_key_id and not (stored and registered):
            raise RuntimeError("The stored key and the key in Yandex differ. Reconcile the provider before rotating it.")
        new_key = None
        try:
            if not (stored and registered):
                if stored or any(k.get("description") == DESCRIPTION for k in keys):
                    raise RuntimeError("An existing provider key needs reconciliation before provisioning")
                new_key = yc("iam", "api-key", "create", "--service-account-id", account_id,
                             "--description", DESCRIPTION, "--scopes", SCOPE)
            model = configure_yandex("gpt://" + args.folder_id + "/" + args.model,
                                     new_key["secret"] if new_key else None)
            settings.update({"yandex_folder_id": args.folder_id, "yandex_account_id": account_id,
                             "yandex_key_id": new_key["api_key"]["id"] if new_key else settings.yandex_key_id})
            settings.save()
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            if new_key:
                yc("iam", "api-key", "delete", "--id", new_key["api_key"]["id"])
            raise
        print("Huf provider ready:", model)
    finally:
        frappe.destroy()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        sys.exit(str(error))
