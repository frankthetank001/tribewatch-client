"""TribeWatch Client — lightweight entry point for distributed builds.

This module provides a client-only CLI that excludes server/standalone modes.
Used by PyInstaller to produce a lean client exe that connects to a remote
TribeWatch server.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tribewatch import __version__


def main() -> None:
    from tribewatch.__main__ import (
        DEFAULT_CONFIG,
        _apply_env_overrides,
        _apply_resolution_preset,
        _check_for_updates,
        _cmd_calibrate,
        _cmd_calibrate_manual,
        _cmd_calibrate_parasaur,
        _cmd_calibrate_tribe,
        _cmd_generate_config,
        _cmd_run_client,
        _cmd_setup,
        _cmd_test_discord,
        _cmd_test_ocr,
        _discover_and_confirm_tribe_name,
        _set_console_title_and_icon,
        _set_dpi_awareness,
        _setup_logging,
    )
    from tribewatch.config import client_config_path, load_config
    from tribewatch.singleton import ensure_single_instance
    from tribewatch.updater import is_frozen

    _set_dpi_awareness()
    _set_console_title_and_icon()

    parser = argparse.ArgumentParser(
        prog="TribeWatch",
        description="TribeWatch Client — ARK: Survival Ascended tribe log monitor",
    )
    parser.add_argument(
        "--version", action="version", version=f"TribeWatch {__version__}"
    )
    parser.add_argument(
        "--config", "-c",
        type=Path,
        default=Path(DEFAULT_CONFIG),
        help=f"Config file path (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Guided setup wizard",
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="Calibrate screen capture region (visual overlay)",
    )
    parser.add_argument(
        "--calibrate-manual",
        action="store_true",
        help="Calibrate screen capture region (manual coordinates)",
    )
    parser.add_argument(
        "--calibrate-parasaur",
        action="store_true",
        help="Calibrate parasaur detection region",
    )
    parser.add_argument(
        "--calibrate-tribe",
        action="store_true",
        help="Calibrate tribe window capture region",
    )
    parser.add_argument(
        "--test-ocr",
        action="store_true",
        help="Capture once, run OCR, print results, exit",
    )
    parser.add_argument(
        "--test-discord",
        action="store_true",
        help="Send test event to configured webhooks",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Run client (used by startup entry / installer)",
    )

    args = parser.parse_args()
    config_path: Path = args.config

    if args.setup:
        # _cmd_setup returns None on success / False on user cancel.
        # Mirror __main__.main() behaviour: fall through after a
        # successful setup so the client launches automatically.
        # Without this, the Start Menu "Setup" shortcut completes
        # calibration and silently exits, leaving the user staring
        # at nothing.
        result = _cmd_setup(config_path)
        if result is False:
            return

    if args.calibrate:
        _cmd_calibrate(config_path)
        return

    if args.calibrate_manual:
        _cmd_calibrate_manual(config_path)
        return

    if args.calibrate_parasaur:
        _cmd_calibrate_parasaur(config_path)
        return

    if args.calibrate_tribe:
        _cmd_calibrate_tribe(config_path)
        return

    # Client mode always uses the client config file
    effective_path = client_config_path(config_path)
    if not effective_path.exists():
        _cmd_generate_config(effective_path, mode="client")

    cfg = load_config(effective_path)
    _apply_env_overrides(cfg)
    # Logging must be configured BEFORE the singleton scan so its
    # warnings (process scan results, kill failures, etc) actually
    # land in tribewatch.log.
    _setup_logging(cfg.general.log_level)

    # Kill any other TribeWatch instance still running. The packaged
    # exe enters via this client_main module — NOT __main__._cmd_run —
    # so the singleton call has to live here too. Without this, two
    # exes can coexist indefinitely with no log output explaining why.
    ensure_single_instance()

    # Prompt for server URL if not set (first launch)
    if not cfg.server.server_url:
        from tribewatch.config import save_config
        print("\n  Welcome to TribeWatch!\n")
        url = input("  Enter your TribeWatch server URL: ").strip()
        if not url:
            print("  No server URL provided. Exiting.")
            input("  Press Enter to close...")
            return
        cfg.server.server_url = url
        save_config(cfg, effective_path, mode="client")
        print(f"  Server URL saved to {effective_path}\n")

    # Auto-update check (frozen builds only)
    if is_frozen():
        _check_for_updates()

    _apply_resolution_preset(cfg)

    # Tribe name discovery
    if cfg.tribe.bbox:
        _discover_and_confirm_tribe_name(cfg, effective_path, mode="client")

    if args.test_ocr:
        _cmd_test_ocr(config_path)
    elif args.test_discord:
        _cmd_test_discord(config_path)
    else:
        _cmd_run_client(cfg, effective_path)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"\n  ERROR: {e}")
        traceback.print_exc()
        input("\n  Press Enter to close...")
