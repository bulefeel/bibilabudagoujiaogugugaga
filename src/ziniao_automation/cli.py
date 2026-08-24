"""Local administration CLI; secret input always uses getpass."""

from __future__ import annotations

import argparse
from getpass import getpass
import json
from pathlib import Path
import sys
from typing import Sequence

from sqlalchemy import select

from .auth import hash_password
from .config import Settings
from .db import create_sqlite_engine, init_database, make_session_factory
from .logging_config import purge_expired_artifacts
from .models import AdminCredential, SystemSetting, ZiniaoAccount
from .ziniao.credentials import (
    DEFAULT_FEISHU_TARGET,
    DEFAULT_ZINIAO_TARGET,
    credential_exists,
    credential_matches,
    delete_generic_credential,
    write_generic_credential,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ziniao-automation")
    sub = parser.add_subparsers(dest="command", required=True)

    configure = sub.add_parser("configure", help="交互配置本地凭据")
    configure_sub = configure.add_subparsers(dest="kind", required=True)
    ziniao = configure_sub.add_parser("ziniao", help="配置紫鸟账号")
    ziniao.add_argument("--credential-ref", default=DEFAULT_ZINIAO_TARGET)
    admin = configure_sub.add_parser("admin", help="配置单一管理员")
    admin.add_argument("--username")
    feishu = configure_sub.add_parser("feishu", help="配置飞书 App")
    feishu.add_argument("--credential-ref", default=DEFAULT_FEISHU_TARGET)
    feishu.add_argument(
        "--reuse-metadata",
        action="store_true",
        help="沿用 SQLite 中的 App ID/Chat ID，只重新输入 App Secret",
    )

    status = sub.add_parser("credentials-status", help="仅显示凭据是否存在")
    status.add_argument("--ziniao-ref", default=DEFAULT_ZINIAO_TARGET)
    status.add_argument("--feishu-ref", default=DEFAULT_FEISHU_TARGET)

    remove = sub.add_parser("delete-credential", help="删除指定本地凭据")
    remove.add_argument("target")

    clean = sub.add_parser("cleanup", help="清理过期日志、截图和报告")
    clean.add_argument("--days", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    settings.ensure_directories()
    if args.command == "configure":
        if args.kind == "ziniao":
            return _configure_ziniao(settings, args.credential_ref)
        if args.kind == "admin":
            return _configure_admin(settings, args.username)
        if args.kind == "feishu":
            return _configure_feishu(
                settings,
                args.credential_ref,
                reuse_metadata=args.reuse_metadata,
            )
    if args.command == "credentials-status":
        print(
            json.dumps(
                {
                    "ziniao": credential_exists(args.ziniao_ref),
                    "feishu": credential_exists(args.feishu_ref),
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "delete-credential":
        deleted = delete_generic_credential(args.target)
        print("凭据已删除" if deleted else "凭据不存在")
        return 0
    if args.command == "cleanup":
        roots = [settings.evidence_dir, settings.log_dir, settings.backup_dir]
        removed = purge_expired_artifacts(
            tuple(path for path in roots if path is not None),
            retention_days=args.days or settings.artifact_retention_days,
        )
        print(f"已清理 {len(removed)} 个过期文件")
        return 0
    return 2


def _configure_ziniao(settings: Settings, credential_ref: str) -> int:
    company = input("紫鸟企业名称: ").strip()
    username = input("紫鸟登录账号: ").strip()
    password = _confirmed_secret("紫鸟登录密码", confirm=False)
    if not company or not username or not password:
        raise ValueError("企业名称、登录账号和密码均为必填")
    write_generic_credential(
        credential_ref,
        {"company": company, "username": username, "password": password},
        username=username,
    )
    if not credential_matches(
        credential_ref,
        {"company": company, "username": username, "password": password},
    ):
        raise RuntimeError("Windows 凭据写入后未能回读，请检查安全软件或凭据管理器")
    engine, sessions = _database(settings)
    try:
        with sessions() as session:
            account = session.scalar(select(ZiniaoAccount).limit(1))
            if account is None:
                account = ZiniaoAccount(
                    display_name="紫鸟主账号",
                    company=company,
                    username=username,
                    credential_ref=credential_ref,
                )
                session.add(account)
            else:
                account.company = company
                account.username = username
                account.credential_ref = credential_ref
                account.enabled = True
            session.commit()
    finally:
        engine.dispose()
    # The password is deliberately neither returned nor printed.
    print(f"紫鸟凭据已保存到 Windows 凭据管理器：{credential_ref}")
    return 0


def _configure_admin(settings: Settings, requested_username: str | None) -> int:
    username = (requested_username or input("管理员用户名: ")).strip()
    password = _confirmed_secret("管理员密码", confirm=True)
    engine, sessions = _database(settings)
    try:
        with sessions() as session:
            existing = session.get(AdminCredential, 1)
            if existing is None:
                session.add(
                    AdminCredential(
                        id=1,
                        username=username,
                        password_hash=hash_password(password),
                    )
                )
            else:
                existing.username = username
                existing.password_hash = hash_password(password)
                existing.session_epoch += 1
            session.commit()
    finally:
        engine.dispose()
    print("单一管理员账号已配置，旧会话已失效")
    return 0


def _configure_feishu(
    settings: Settings,
    credential_ref: str,
    *,
    reuse_metadata: bool = False,
) -> int:
    if reuse_metadata:
        existing = _read_feishu_metadata(settings)
        app_id = str(existing.get("app_id", "")).strip()
        chat_id = str(existing.get("chat_id", "")).strip()
        credential_ref = str(
            existing.get("credential_ref", credential_ref)
        ).strip()
        if not app_id or not chat_id or not credential_ref:
            raise ValueError("SQLite 中没有可复用的飞书 App ID/Chat ID")
    else:
        app_id = input("飞书 App ID: ").strip()
        chat_id = ""
    app_secret = _confirmed_secret("飞书 App Secret", confirm=False)
    if not reuse_metadata:
        chat_id = input("飞书 Chat ID: ").strip()
    if not app_id or not app_secret or not chat_id:
        raise ValueError("App ID、App Secret 和 Chat ID 均为必填")
    write_generic_credential(
        credential_ref,
        {"app_id": app_id, "app_secret": app_secret, "chat_id": chat_id},
        username=app_id,
    )
    if not credential_matches(
        credential_ref,
        {"app_id": app_id, "app_secret": app_secret, "chat_id": chat_id},
    ):
        raise RuntimeError("Windows 凭据写入后未能回读，请检查安全软件或凭据管理器")
    engine, sessions = _database(settings)
    try:
        with sessions() as session:
            row = session.get(SystemSetting, "feishu")
            metadata = {
                "credential_ref": credential_ref,
                "app_id": app_id,
                "chat_id": chat_id,
                "enabled": True,
            }
            if row is None:
                session.add(SystemSetting(key="feishu", value=metadata))
            else:
                row.value = metadata
            session.commit()
    finally:
        engine.dispose()
    print(f"飞书配置已保存；敏感凭据位于 Windows 凭据管理器：{credential_ref}")
    return 0


def _read_feishu_metadata(settings: Settings) -> dict[str, object]:
    engine, sessions = _database(settings)
    try:
        with sessions() as session:
            row = session.get(SystemSetting, "feishu")
            return dict(row.value) if row is not None and row.value else {}
    finally:
        engine.dispose()


def _confirmed_secret(label: str, *, confirm: bool) -> str:
    value = getpass(f"{label}: ")
    if confirm:
        repeated = getpass(f"再次输入{label}: ")
        if value != repeated:
            raise ValueError("两次输入不一致")
    return value


def _database(settings: Settings):
    engine = create_sqlite_engine(settings)
    init_database(engine)
    return engine, make_session_factory(engine)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as exc:
        print(f"配置失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from None
