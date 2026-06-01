"""Native macOS setup wizard for the OpenShrimp menu bar app.

Uses AppKit (via PyObjC) to present a proper multi-step wizard window
with inline validation, async token verification, and native folder
selection.  Replaces the previous chain of ``rumps.Window`` alerts.
"""

from __future__ import annotations

import logging
import random
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import objc
from AppKit import (
    NSAlert,
    NSApplication,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSCenterTextAlignment,
    NSColor,
    NSFont,
    NSLineBreakByTruncatingMiddle,
    NSMenu,
    NSMenuItem,
    NSModalResponseOK,
    NSOpenPanel,
    NSPopUpButton,
    NSProgressIndicator,
    NSProgressIndicatorSpinningStyle,
    NSSecureTextField,
    NSSmallControlSize,
    NSTextField,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
    NSWorkspace,
)
from Foundation import (
    NSMakeRect,
    NSObject,
    NSURL,
)

from open_shrimp.config import DEFAULT_CONFIG_PATH, write_config

logger = logging.getLogger("open_shrimp.app_setup")

# ── Constants ──

_WINDOW_WIDTH = 480
_WINDOW_HEIGHT = 380
_CONTENT_INSET = 32
_CONTENT_WIDTH = _WINDOW_WIDTH - 2 * _CONTENT_INSET

_MODELS: tuple[tuple[str, str], ...] = (
    ("openai/gpt-5.5", "OpenAI GPT-5.5"),
    ("anthropic/claude-sonnet-4-6", "Anthropic Claude Sonnet"),
)

# Module-level reference to prevent GC of the active controller.
_active_controller: Any = None


# ── Helpers ──


def _make_label(
    text: str,
    frame: tuple[float, float, float, float],
    *,
    font: Any | None = None,
    color: Any | None = None,
    alignment: int | None = None,
) -> NSTextField:
    """Create a non-editable label."""
    label = NSTextField.alloc().initWithFrame_(NSMakeRect(*frame))
    label.setStringValue_(text)
    label.setBezeled_(False)
    label.setDrawsBackground_(False)
    label.setEditable_(False)
    label.setSelectable_(False)
    if font:
        label.setFont_(font)
    if color:
        label.setTextColor_(color)
    if alignment is not None:
        label.setAlignment_(alignment)
    return label


def _make_text_field(
    frame: tuple[float, float, float, float],
    placeholder: str = "",
    *,
    secure: bool = False,
) -> NSTextField:
    """Create an editable text field."""
    cls = NSSecureTextField if secure else NSTextField
    tf = cls.alloc().initWithFrame_(NSMakeRect(*frame))
    tf.setPlaceholderString_(placeholder)
    return tf


def _make_link_button(
    title: str,
    frame: tuple[float, float, float, float],
    target: Any,
    action: str,
) -> NSButton:
    """Create a borderless link-style button."""
    btn = NSButton.alloc().initWithFrame_(NSMakeRect(*frame))
    btn.setTitle_(title)
    btn.setBordered_(False)
    btn.setFont_(NSFont.systemFontOfSize_(12))
    # Use accent/link color
    btn.setContentTintColor_(NSColor.linkColor() if hasattr(NSColor, "linkColor") else NSColor.blueColor())
    btn.setTarget_(target)
    btn.setAction_(action)
    return btn


def _make_spinner(frame: tuple[float, float, float, float]) -> NSProgressIndicator:
    """Create a small indeterminate spinner, initially hidden."""
    spinner = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(*frame))
    spinner.setStyle_(NSProgressIndicatorSpinningStyle)
    spinner.setControlSize_(NSSmallControlSize)
    spinner.setDisplayedWhenStopped_(False)
    return spinner


def _validate_token_format(token: str) -> str | None:
    """Return an error string if *token* doesn't look like a bot token."""
    if not token:
        return "Bot token is required."
    if ":" not in token:
        return "Token should look like '123456:ABC-DEF…' — get one from @BotFather."
    return None


def _validate_user_id(text: str) -> str | None:
    """Return an error string if *text* is not a valid Telegram user ID."""
    if not text:
        return "User ID is required."
    try:
        uid = int(text)
    except ValueError:
        return "Must be a number."
    if uid <= 0:
        return "Must be a positive number."
    return None


def _validate_context_name(name: str) -> str | None:
    """Return an error string if *name* isn't a valid context name."""
    if not name:
        return "Context name is required."
    if not name.replace("-", "").replace("_", "").isalnum():
        return "Use only letters, numbers, hyphens, and underscores."
    return None


def _build_config_dict(
    token: str,
    user_id: int,
    context_name: str,
    directory: str,
    description: str,
    model: str | None,
) -> dict[str, Any]:
    """Assemble the config dictionary for YAML serialisation."""
    context: dict[str, Any] = {
        "directory": directory,
        "description": description,
        "allowed_tools": ["LSP"],
    }
    if model is not None:
        context["model"] = model

    return {
        "telegram": {"token": token},
        "allowed_users": [user_id],
        "contexts": {context_name: context},
        "default_context": context_name,
        "review": {
            "port": random.randint(49152, 65535),
            "tunnel": "cloudflared",
        },
    }


# ── Custom Window ──


class _EditableWindow(NSWindow):
    """NSWindow subclass that routes standard editing shortcuts to the
    first responder, working around rumps not setting up an Edit menu."""

    _CMD_KEYS = {
        "v": "paste:",
        "c": "copy:",
        "x": "cut:",
        "a": "selectAll:",
        "z": "undo:",
    }
    _CMD_SHIFT_KEYS = {
        "z": "redo:",
    }

    _NSCommandKeyMask = 1 << 20
    _NSShiftKeyMask = 1 << 17

    def performKeyEquivalent_(self, event: Any) -> bool:
        flags = event.modifierFlags()
        if flags & self._NSCommandKeyMask:
            chars = event.charactersIgnoringModifiers()
            if not chars:
                return objc.super(_EditableWindow, self).performKeyEquivalent_(event)

            # Cmd+Shift combos
            if flags & self._NSShiftKeyMask:
                action = self._CMD_SHIFT_KEYS.get(chars.lower())
            else:
                action = self._CMD_KEYS.get(chars)

            if action:
                responder = self.firstResponder()
                if responder and responder.respondsToSelector_(action):
                    NSApplication.sharedApplication().sendAction_to_from_(
                        action, responder, self,
                    )
                    return True
        return objc.super(_EditableWindow, self).performKeyEquivalent_(event)


# ── Wizard Controller ──


class SetupWizardController(NSObject):
    """Drives the multi-step native setup wizard window."""

    def init(self):  # noqa: D401 — NSObject init
        self = objc.super(SetupWizardController, self).init()
        if self is None:
            return None

        self._current_step: int = 0
        self._num_steps: int = 3
        self._finished: bool = False
        self._on_complete: Callable[[], None] = lambda: None
        self._on_cancel: Callable[[], None] = lambda: None

        # Collected data
        self._verified_bot_name: str | None = None
        self._selected_directory: str | None = None

        # UI references (set during build)
        self._window: NSWindow | None = None
        self._step_views: list[NSView] = []
        self._dots: list[NSTextField] = []
        self._back_button: NSButton | None = None
        self._next_button: NSButton | None = None

        # Step 0
        self._token_field: NSTextField | None = None
        self._token_error: NSTextField | None = None
        self._token_success: NSTextField | None = None
        self._token_spinner: NSProgressIndicator | None = None

        # Step 1
        self._userid_field: NSTextField | None = None
        self._userid_error: NSTextField | None = None

        # Step 2
        self._folder_label: NSTextField | None = None
        self._context_name_field: NSTextField | None = None
        self._model_popup: NSPopUpButton | None = None
        self._custom_model_field: NSTextField | None = None
        self._custom_model_label: NSTextField | None = None
        self._context_error: NSTextField | None = None

        return self

    # ── Public API ──

    @objc.python_method
    def show(
        self,
        on_complete: Callable[[], None],
        on_cancel: Callable[[], None],
    ) -> None:
        self._on_complete = on_complete
        self._on_cancel = on_cancel
        self._ensure_edit_menu()
        self._build_window()
        self._window.makeKeyAndOrderFront_(None)

    @objc.python_method
    def _ensure_edit_menu(self) -> None:
        """Ensure the app has an Edit menu so Cmd+C/V/X/A work in text fields."""
        app = NSApplication.sharedApplication()
        main_menu = app.mainMenu()
        if main_menu is None:
            return

        # Check if Edit menu already exists
        for i in range(main_menu.numberOfItems()):
            item = main_menu.itemAtIndex_(i)
            if item.title() == "Edit":
                return

        edit_menu = NSMenu.alloc().initWithTitle_("Edit")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Undo", "undo:", "z")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Redo", "redo:", "Z")
        edit_menu.addItem_(NSMenuItem.separatorItem())
        edit_menu.addItemWithTitle_action_keyEquivalent_("Cut", "cut:", "x")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Copy", "copy:", "c")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Paste", "paste:", "v")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Select All", "selectAll:", "a")

        edit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Edit", None, "")
        edit_item.setSubmenu_(edit_menu)
        main_menu.addItem_(edit_item)

    # ── Window Construction ──

    @objc.python_method
    def _build_window(self) -> None:
        w = _EditableWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, _WINDOW_WIDTH, _WINDOW_HEIGHT),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered,
            False,
        )
        w.setTitle_("OpenShrimp Setup")
        w.center()
        w.setDelegate_(self)
        # Prevent resizing
        w.setShowsResizeIndicator_(False)
        w.setMinSize_(w.frame().size)
        w.setMaxSize_(w.frame().size)
        self._window = w

        content = w.contentView()

        # Build step views
        step_frame = (_CONTENT_INSET, 72, _CONTENT_WIDTH, 248)
        self._step_views = [
            self._build_step_token(step_frame),
            self._build_step_userid(step_frame),
            self._build_step_context(step_frame),
        ]
        # Show only step 0 initially
        for i, view in enumerate(self._step_views):
            content.addSubview_(view)
            view.setHidden_(i != 0)

        # Step indicator dots
        self._build_dots(content)

        # Navigation buttons
        self._build_nav_buttons(content)

        self._update_ui()

    @objc.python_method
    def _build_dots(self, parent: NSView) -> None:
        dot_y = 46
        dot_spacing = 20
        total_width = self._num_steps * dot_spacing
        start_x = (_WINDOW_WIDTH - total_width) / 2

        for i in range(self._num_steps):
            dot = _make_label(
                "●" if i == 0 else "○",
                (start_x + i * dot_spacing, dot_y, dot_spacing, 16),
                font=NSFont.systemFontOfSize_(10),
                alignment=NSCenterTextAlignment,
            )
            parent.addSubview_(dot)
            self._dots.append(dot)

    @objc.python_method
    def _build_nav_buttons(self, parent: NSView) -> None:
        self._back_button = NSButton.alloc().initWithFrame_(
            NSMakeRect(_CONTENT_INSET, 12, 80, 28)
        )
        self._back_button.setTitle_("Back")
        self._back_button.setBezelStyle_(NSBezelStyleRounded)
        self._back_button.setTarget_(self)
        self._back_button.setAction_("goBack:")
        parent.addSubview_(self._back_button)

        self._next_button = NSButton.alloc().initWithFrame_(
            NSMakeRect(_WINDOW_WIDTH - _CONTENT_INSET - 80, 12, 80, 28)
        )
        self._next_button.setTitle_("Next")
        self._next_button.setBezelStyle_(NSBezelStyleRounded)
        self._next_button.setTarget_(self)
        self._next_button.setAction_("goNext:")
        self._next_button.setKeyEquivalent_("\r")  # Enter key
        parent.addSubview_(self._next_button)

    # ── Step 0: Bot Token ──

    @objc.python_method
    def _build_step_token(self, frame: tuple[float, float, float, float]) -> NSView:
        view = NSView.alloc().initWithFrame_(NSMakeRect(*frame))
        w = frame[2]
        y = frame[3]  # build top-down

        y -= 28
        view.addSubview_(_make_label(
            "Telegram Bot Token",
            (0, y, w, 24),
            font=NSFont.boldSystemFontOfSize_(16),
        ))

        y -= 22
        view.addSubview_(_make_label(
            "Paste the token you received from @BotFather.",
            (0, y, w, 18),
            font=NSFont.systemFontOfSize_(12),
            color=NSColor.secondaryLabelColor(),
        ))

        y -= 22
        link = _make_link_button(
            "Open @BotFather in Telegram →",
            (0, y, 220, 18),
            target=self,
            action="openBotFather:",
        )
        view.addSubview_(link)

        y -= 32
        self._token_field = _make_text_field(
            (0, y, w, 24),
            placeholder="123456:ABC-DEF…",
            secure=True,
        )
        view.addSubview_(self._token_field)

        y -= 22
        self._token_error = _make_label(
            "",
            (0, y, w, 18),
            font=NSFont.systemFontOfSize_(11),
            color=NSColor.systemRedColor(),
        )
        view.addSubview_(self._token_error)

        self._token_success = _make_label(
            "",
            (0, y, w - 24, 18),
            font=NSFont.systemFontOfSize_(11),
            color=NSColor.systemGreenColor(),
        )
        self._token_success.setHidden_(True)
        view.addSubview_(self._token_success)

        self._token_spinner = _make_spinner((w - 20, y, 16, 16))
        view.addSubview_(self._token_spinner)

        return view

    # ── Step 1: User ID ──

    @objc.python_method
    def _build_step_userid(self, frame: tuple[float, float, float, float]) -> NSView:
        view = NSView.alloc().initWithFrame_(NSMakeRect(*frame))
        w = frame[2]
        y = frame[3]

        y -= 28
        view.addSubview_(_make_label(
            "Your Telegram User ID",
            (0, y, w, 24),
            font=NSFont.boldSystemFontOfSize_(16),
        ))

        y -= 22
        view.addSubview_(_make_label(
            "Send /start to @userinfobot on Telegram to find your user ID.",
            (0, y, w, 18),
            font=NSFont.systemFontOfSize_(12),
            color=NSColor.secondaryLabelColor(),
        ))

        y -= 22
        link = _make_link_button(
            "Open @userinfobot in Telegram →",
            (0, y, 230, 18),
            target=self,
            action="openUserInfoBot:",
        )
        view.addSubview_(link)

        y -= 32
        self._userid_field = _make_text_field(
            (0, y, 180, 24),
            placeholder="e.g. 123456789",
        )
        view.addSubview_(self._userid_field)

        y -= 22
        self._userid_error = _make_label(
            "",
            (0, y, w, 18),
            font=NSFont.systemFontOfSize_(11),
            color=NSColor.systemRedColor(),
        )
        view.addSubview_(self._userid_error)

        return view

    # ── Step 2: Context ──

    @objc.python_method
    def _build_step_context(self, frame: tuple[float, float, float, float]) -> NSView:
        view = NSView.alloc().initWithFrame_(NSMakeRect(*frame))
        w = frame[2]
        y = frame[3]

        y -= 28
        view.addSubview_(_make_label(
            "Your First Context",
            (0, y, w, 24),
            font=NSFont.boldSystemFontOfSize_(16),
        ))

        y -= 22
        view.addSubview_(_make_label(
            "A context links a project folder to a name you can switch to in Telegram.",
            (0, y, w, 18),
            font=NSFont.systemFontOfSize_(12),
            color=NSColor.secondaryLabelColor(),
        ))

        # Project folder
        y -= 28
        view.addSubview_(_make_label(
            "Project folder",
            (0, y, 100, 18),
            font=NSFont.systemFontOfSize_(12),
        ))

        y -= 24
        choose_btn = NSButton.alloc().initWithFrame_(NSMakeRect(0, y, 110, 24))
        choose_btn.setTitle_("Choose Folder…")
        choose_btn.setBezelStyle_(NSBezelStyleRounded)
        choose_btn.setTarget_(self)
        choose_btn.setAction_("chooseFolder:")
        view.addSubview_(choose_btn)

        self._folder_label = _make_label(
            "No folder selected",
            (118, y + 3, w - 118, 18),
            font=NSFont.systemFontOfSize_(11),
            color=NSColor.secondaryLabelColor(),
        )
        self._folder_label.setLineBreakMode_(NSLineBreakByTruncatingMiddle)
        view.addSubview_(self._folder_label)

        # Context name
        y -= 28
        view.addSubview_(_make_label(
            "Context name",
            (0, y, 100, 18),
            font=NSFont.systemFontOfSize_(12),
        ))

        y -= 24
        self._context_name_field = _make_text_field(
            (0, y, 200, 24),
            placeholder="e.g. my-project",
        )
        self._context_name_field.setStringValue_("default")
        view.addSubview_(self._context_name_field)

        # Model
        y -= 28
        view.addSubview_(_make_label(
            "Model",
            (0, y, 100, 18),
            font=NSFont.systemFontOfSize_(12),
        ))

        y -= 26
        self._model_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(0, y, 250, 24),
            False,
        )
        for model_name, model_desc in _MODELS:
            self._model_popup.addItemWithTitle_(f"{model_name} — {model_desc}")
        self._model_popup.addItemWithTitle_("Custom…")
        self._model_popup.setTarget_(self)
        self._model_popup.setAction_("modelChanged:")
        view.addSubview_(self._model_popup)

        # Custom model field (hidden by default)
        y -= 26
        self._custom_model_label = _make_label(
            "Custom provider/model",
            (0, y + 2, 130, 18),
            font=NSFont.systemFontOfSize_(12),
        )
        self._custom_model_label.setHidden_(True)
        view.addSubview_(self._custom_model_label)

        self._custom_model_field = _make_text_field(
            (130, y, w - 130, 24),
            placeholder="e.g. openai/gpt-5.5",
        )
        self._custom_model_field.setHidden_(True)
        view.addSubview_(self._custom_model_field)

        # Error
        self._context_error = _make_label(
            "",
            (0, y - 20, w, 18),
            font=NSFont.systemFontOfSize_(11),
            color=NSColor.systemRedColor(),
        )
        view.addSubview_(self._context_error)

        return view

    # ── Navigation ──

    def goBack_(self, sender: Any) -> None:
        if self._current_step > 0:
            self._current_step -= 1
            self._show_current_step()

    def goNext_(self, sender: Any) -> None:
        if not self._validate_current_step():
            return

        if self._current_step == 0:
            # Async token verification before advancing
            self._verify_token()
            return

        if self._current_step == self._num_steps - 1:
            self._finish()
            return

        self._current_step += 1
        self._show_current_step()

    @objc.python_method
    def _show_current_step(self) -> None:
        for i, view in enumerate(self._step_views):
            view.setHidden_(i != self._current_step)
        self._update_ui()

    @objc.python_method
    def _update_ui(self) -> None:
        # Dots
        for i, dot in enumerate(self._dots):
            dot.setStringValue_("●" if i == self._current_step else "○")
            if i == self._current_step:
                dot.setTextColor_(NSColor.labelColor())
            else:
                dot.setTextColor_(NSColor.tertiaryLabelColor())

        # Buttons
        self._back_button.setHidden_(self._current_step == 0)
        if self._current_step == self._num_steps - 1:
            self._next_button.setTitle_("Finish")
        else:
            self._next_button.setTitle_("Next")

    # ── Validation ──

    @objc.python_method
    def _validate_current_step(self) -> bool:
        if self._current_step == 0:
            return self._validate_step_token()
        elif self._current_step == 1:
            return self._validate_step_userid()
        elif self._current_step == 2:
            return self._validate_step_context()
        return True

    @objc.python_method
    def _validate_step_token(self) -> bool:
        token = self._token_field.stringValue().strip()
        err = _validate_token_format(token)
        if err:
            self._token_error.setStringValue_(err)
            self._token_error.setHidden_(False)
            self._token_success.setHidden_(True)
            return False
        self._token_error.setStringValue_("")
        return True

    @objc.python_method
    def _validate_step_userid(self) -> bool:
        text = self._userid_field.stringValue().strip()
        err = _validate_user_id(text)
        if err:
            self._userid_error.setStringValue_(err)
            return False
        self._userid_error.setStringValue_("")
        return True

    @objc.python_method
    def _validate_step_context(self) -> bool:
        if not self._selected_directory:
            self._context_error.setStringValue_("Please choose a project folder.")
            return False

        name = self._context_name_field.stringValue().strip()
        err = _validate_context_name(name)
        if err:
            self._context_error.setStringValue_(err)
            return False

        # Check custom model field if "Custom…" is selected
        if self._model_popup.indexOfSelectedItem() == len(_MODELS):
            custom = self._custom_model_field.stringValue().strip()
            if not custom:
                self._context_error.setStringValue_("Enter a custom provider/model.")
                return False

        self._context_error.setStringValue_("")
        return True

    # ── Async Token Verification ──

    @objc.python_method
    def _verify_token(self) -> None:
        """Verify the bot token via the Telegram getMe API."""
        token = self._token_field.stringValue().strip()

        # If already verified with this token, advance
        if self._verified_bot_name is not None:
            self._current_step += 1
            self._show_current_step()
            return

        self._token_error.setHidden_(True)
        self._token_success.setHidden_(True)
        self._token_spinner.startAnimation_(None)
        self._next_button.setEnabled_(False)

        def _check() -> None:
            try:
                r = httpx.get(
                    f"https://api.telegram.org/bot{token}/getMe",
                    timeout=10,
                )
                data = r.json()
                if data.get("ok"):
                    bot_name = data["result"].get("username", "unknown")
                    self._verified_bot_name = bot_name
                    self.performSelectorOnMainThread_withObject_waitUntilDone_(
                        "onTokenVerified:", f"@{bot_name}", False,
                    )
                else:
                    desc = data.get("description", "Invalid token")
                    self.performSelectorOnMainThread_withObject_waitUntilDone_(
                        "onTokenFailed:", desc, False,
                    )
            except Exception as exc:
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "onTokenFailed:", str(exc), False,
                )

        threading.Thread(target=_check, daemon=True).start()

    def onTokenVerified_(self, bot_name: str) -> None:
        self._token_spinner.stopAnimation_(None)
        self._next_button.setEnabled_(True)
        self._token_error.setHidden_(True)
        self._token_success.setStringValue_(f"Found {bot_name}")
        self._token_success.setHidden_(False)
        # Advance to next step
        self._current_step += 1
        self._show_current_step()

    def onTokenFailed_(self, error: str) -> None:
        self._token_spinner.stopAnimation_(None)
        self._next_button.setEnabled_(True)
        self._token_success.setHidden_(True)
        self._token_error.setStringValue_(error)
        self._token_error.setHidden_(False)

    # ── Model Selection ──

    def modelChanged_(self, sender: Any) -> None:
        is_custom = sender.indexOfSelectedItem() == len(_MODELS)
        self._custom_model_field.setHidden_(not is_custom)
        self._custom_model_label.setHidden_(not is_custom)

    @objc.python_method
    def _get_selected_model(self) -> str | None:
        idx = self._model_popup.indexOfSelectedItem()
        if idx == len(_MODELS):
            # Custom
            return self._custom_model_field.stringValue().strip() or None
        return _MODELS[idx][0]

    # ── Folder Picker ──

    def chooseFolder_(self, sender: Any) -> None:
        panel = NSOpenPanel.openPanel()
        panel.setTitle_("Choose a project folder")
        panel.setCanChooseFiles_(False)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(False)
        panel.setCanCreateDirectories_(False)

        if panel.runModal() == NSModalResponseOK:
            url = panel.URLs()[0]
            path = url.path()
            self._selected_directory = str(path)
            self._folder_label.setStringValue_(str(path))
            self._folder_label.setTextColor_(NSColor.labelColor())

    # ── Deep Links ──

    def openBotFather_(self, sender: Any) -> None:
        NSWorkspace.sharedWorkspace().openURL_(
            NSURL.URLWithString_("tg://resolve?domain=BotFather")
        )

    def openUserInfoBot_(self, sender: Any) -> None:
        NSWorkspace.sharedWorkspace().openURL_(
            NSURL.URLWithString_("tg://resolve?domain=userinfobot")
        )

    # ── Finish ──

    @objc.python_method
    def _finish(self) -> None:
        token = self._token_field.stringValue().strip()
        user_id = int(self._userid_field.stringValue().strip())
        context_name = self._context_name_field.stringValue().strip()
        directory = self._selected_directory
        model = self._get_selected_model()

        config_dict = _build_config_dict(
            token=token,
            user_id=user_id,
            context_name=context_name,
            directory=directory,
            description="Default context",
            model=model,
        )

        config_path = Path(DEFAULT_CONFIG_PATH)
        try:
            write_config(config_path, config_dict)
        except OSError as exc:
            alert = NSAlert.alloc().init()
            alert.setMessageText_("Failed to write config")
            alert.setInformativeText_(str(exc))
            alert.runModal()
            return

        self._finished = True

        # Show confirmation while the window is still alive.
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Setup Complete")
        bot_name = self._verified_bot_name or "your bot"
        alert.setInformativeText_(
            f"Config saved to:\n{config_path}\n\n"
            f"Bot @{bot_name} will now start. "
            "Send it a message on Telegram to try it out!"
        )
        alert.addButtonWithTitle_("OK")
        alert.runModal()

        # Hide the window instead of closing — avoids segfault from
        # tearing down the view hierarchy during modal session cleanup.
        self._window.orderOut_(None)
        self._window.setDelegate_(None)

        on_complete = self._on_complete
        _release_controller()
        on_complete()

    # ── Window Delegate ──

    def windowWillClose_(self, notification: Any) -> None:
        if not self._finished:
            self._on_cancel()
            _release_controller()


# ── Public API ──


def _release_controller() -> None:
    global _active_controller
    _active_controller = None


def run_setup_wizard(
    on_complete: Callable[[], None],
    on_cancel: Callable[[], None],
) -> None:
    """Show the native setup wizard window.

    The wizard runs non-blocking.  *on_complete* is called after config is
    written successfully.  *on_cancel* is called if the user closes the
    window without finishing.
    """
    global _active_controller
    controller = SetupWizardController.alloc().init()
    _active_controller = controller  # prevent GC
    controller.show(on_complete=on_complete, on_cancel=on_cancel)
