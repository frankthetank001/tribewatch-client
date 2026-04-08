"""Reusable overlay-style modal dialog for client-side action prompts.

Matches the visual style of the calibration overlay (dark `#1a1a1a`
card with a gold `#FFD700` border, white Segoe UI text, green primary
button + gray secondary buttons), but without the rectangle-drawing
canvas — purely a confirmation / action picker.

The single public entry point is :func:`show_action_dialog`. It runs
its own ``tk.Tk`` mainloop and is therefore safe to call from any
context (sync OR ``run_in_executor`` from an async caller).
"""

from __future__ import annotations

from typing import Sequence


def show_action_dialog(
    title: str,
    message: str,
    buttons: Sequence[tuple[str, str]],
    *,
    default: str = "",
    fullscreen_backdrop: bool = True,
) -> str:
    """Show a modal action dialog matching the calibration overlay style.

    Args:
        title: Heading text shown above the message in larger bold font.
        message: Body text. Newlines are honoured; long lines wrap.
        buttons: List of ``(label, value)`` tuples. The first button is
            styled as the primary action (green). All others are styled
            as secondary (gray). The clicked button's value is returned.
        default: Optional value of the button to bind to ``<Return>`` and
            give initial keyboard focus.
        fullscreen_backdrop: When True (default), draw a 30%-opacity
            black fullscreen layer behind the card so the dialog feels
            like a modal over the game. Set False for a plain centred
            popup with no backdrop (useful for tests / quick prompts).

    Returns:
        The ``value`` of the clicked button, or empty string if the
        dialog is closed (Escape, window manager close).
    """
    import tkinter as tk

    result = {"value": ""}

    # --- Backdrop (optional) ---
    if fullscreen_backdrop:
        root = tk.Tk()
        root.title(title)
        try:
            root.attributes("-fullscreen", True)
            root.attributes("-alpha", 0.3)
            root.attributes("-topmost", True)
            root.overrideredirect(True)
        except Exception:
            pass
        root.configure(bg="black")
        # Click-through is not portable in tkinter, so just absorb clicks
        # outside the card silently.
    else:
        root = tk.Tk()
        root.withdraw()  # invisible host so the Toplevel has a parent

    # --- Card (Toplevel) ---
    top = tk.Toplevel(root)
    top.overrideredirect(True)
    try:
        top.attributes("-topmost", True)
    except Exception:
        pass
    top.configure(bg="#1a1a1a")

    # Gold border via nested frames
    border = tk.Frame(top, bg="#FFD700", padx=3, pady=3)
    border.pack(fill=tk.BOTH, expand=True)
    inner = tk.Frame(border, bg="#1a1a1a", padx=28, pady=22)
    inner.pack(fill=tk.BOTH, expand=True)

    # Title
    tk.Label(
        inner, text=title,
        fg="white", bg="#1a1a1a",
        font=("Segoe UI", 14, "bold"),
        justify="center",
    ).pack(pady=(0, 12))

    # Body message
    tk.Label(
        inner, text=message,
        fg="#e0e0e0", bg="#1a1a1a",
        font=("Segoe UI", 11),
        justify="left",
        wraplength=620,
    ).pack(pady=(0, 18))

    # Button row
    btn_frame = tk.Frame(inner, bg="#1a1a1a")
    btn_frame.pack()

    def _make_cb(value: str):
        def _cb() -> None:
            result["value"] = value
            try:
                top.destroy()
            except Exception:
                pass
            try:
                root.destroy()
            except Exception:
                pass
        return _cb

    default_btn = None
    for idx, (label, value) in enumerate(buttons):
        if idx == 0:
            # Primary action — green
            bg, active = "#225522", "#337733"
        else:
            bg, active = "#444444", "#666666"
        b = tk.Button(
            btn_frame, text=label,
            command=_make_cb(value),
            font=("Segoe UI", 11, "bold"),
            bg=bg, fg="white", activebackground=active,
            relief=tk.RAISED, bd=2, padx=14, pady=6,
        )
        b.pack(side=tk.LEFT, padx=8)
        if value == default:
            default_btn = b

    # Escape closes (returns empty)
    top.bind("<Escape>", lambda _e: _make_cb("")())
    if default_btn is not None:
        default_btn.focus_set()
        top.bind("<Return>", lambda _e, b=default_btn: b.invoke())

    # --- Center the card on screen ---
    top.update_idletasks()
    w = top.winfo_reqwidth()
    h = top.winfo_reqheight()
    sw = top.winfo_screenwidth()
    sh = top.winfo_screenheight()
    x = (sw - w) // 2
    y = (sh - h) // 2
    top.geometry(f"{w}x{h}+{x}+{y}")

    # Bring the card above the (semi-transparent) backdrop
    top.lift()
    top.focus_force()
    try:
        top.grab_set()
    except Exception:
        pass

    root.mainloop()
    return result["value"]
