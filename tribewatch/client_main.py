"""TribeWatch Client — lightweight entry point for distributed builds.

This module provides a client-only CLI that excludes server/standalone modes.
Used by PyInstaller to produce a lean client exe that connects to a remote
TribeWatch server.
"""

from __future__ import annotations

import argparse
import sys
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
        _cmd_reset_all,
        _cmd_reset_calibration,
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

    # Frozen (PyInstaller) builds: ensure CWD is the install directory so
    # relative paths (config, state files, debug screenshots) resolve
    # correctly even when launched from a startup shortcut or scheduled
    # task where Windows sets CWD to System32.
    if is_frozen():
        import os
        install_dir = Path(sys.executable).parent
        os.chdir(install_dir)

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
        "--reset-calibration",
        action="store_true",
        help="Reset screen regions to resolution defaults (discards manual calibration)",
    )
    parser.add_argument(
        "--reset-all",
        action="store_true",
        help="Full reset: deletes client config, calibration, dedup state, and local caches",
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

    # Kill any other TribeWatch instance still running BEFORE running
    # any wizard / calibration GUI. Otherwise the post-install launch
    # and a Start Menu "Setup" shortcut will coexist for the duration
    # of the wizard, leaving the user staring at two windows.
    ensure_single_instance()

    if args.reset_calibration:
        _cmd_reset_calibration(effective_path)
        return

    if args.reset_all:
        _cmd_reset_all(effective_path)
        return

    if args.setup:
        # _cmd_setup returns None on success / False on user cancel.
        # Mirror __main__.main() behaviour: fall through after a
        # successful setup so the client launches automatically.
        # Without this, the Start Menu "Setup" shortcut completes
        # calibration and silently exits, leaving the user staring
        # at nothing.
        result = _cmd_setup(effective_path)
        if result is False:
            return
        # Reload config so the wizard's writes are picked up
        cfg = load_config(effective_path)
        _apply_env_overrides(cfg)

    if args.calibrate:
        _cmd_calibrate(effective_path)
        return

    if args.calibrate_manual:
        _cmd_calibrate_manual(effective_path)
        return

    if args.calibrate_parasaur:
        _cmd_calibrate_parasaur(effective_path)
        return

    if args.calibrate_tribe:
        _cmd_calibrate_tribe(effective_path)
        return

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

    verified = _apply_resolution_preset(cfg)
    if not verified and not args.setup:
        # Same forced-setup gate as __main__._cmd_run: derived presets
        # for unverified resolutions are naive scaled-from-1080p guesses
        # and are usually way off for non-16:9 aspect ratios. Force the
        # user through the calibration wizard before running.
        try:
            from tribewatch.server_id import get_game_resolution
            res = get_game_resolution()
        except Exception:
            res = None
        res_str = f"{res[0]}x{res[1]}" if res else "your current"
        print()
        print("=" * 70)
        print(f"  Unverified resolution: {res_str}")
        print("=" * 70)
        print(
            "  TribeWatch derived capture regions for this resolution from the\n"
            "  1920x1080 baseline, but it has not been hand-verified. The setup\n"
            "  wizard will now open so you can confirm or adjust the regions."
        )
        print("=" * 70)
        print()
        try:
            _cmd_setup(effective_path)
            cfg = load_config(effective_path)
            _apply_env_overrides(cfg)
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).exception("Forced setup wizard failed")

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
