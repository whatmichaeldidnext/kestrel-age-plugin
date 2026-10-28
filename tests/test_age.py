"""Tests for the ported `age` plugin, driven through the real kestrel framework
(Loader, PluginContext, StubUI, HandshakeEvent) with no hardware."""

import importlib.util
import json
import logging
import os
import types

from kestrel_pi.capture import CaptureInterface, HandshakeEvent
from kestrel_pi.connectivity import ConnectivityState
from kestrel_pi.context import PluginContext, ServiceRegistry
from kestrel_pi.declarations import read_declarations
from kestrel_pi.loader import Loader
from kestrel_pi.plugins import PluginPage, PluginRedirect
from kestrel_pi.ui import StubUI

PLUGIN_DIR = os.path.join(os.path.dirname(__file__), "..", "plugins")
AGE_PY = os.path.normpath(os.path.join(PLUGIN_DIR, "age.py"))


def _mod():
    spec = importlib.util.spec_from_file_location("age_under_test", AGE_PY)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


AGE = _mod()


def _plugin(tmp_path, options=None):
    opts = dict(options or {})
    opts.setdefault("data_path", os.path.join(str(tmp_path), "age.json"))
    p = AGE.Age()
    p.options = opts
    p.context = PluginContext(
        name="age", options=opts,
        capture=CaptureInterface(str(tmp_path)),
        connectivity=types.SimpleNamespace(online=False),
        services=ServiceRegistry(), capabilities=("ui", "web"),
    )
    p.on_loaded()
    return p


def _hs(htype="eapol", bssid="AA:BB:CC:DD:EE:01", essid="net"):
    return HandshakeEvent(path="x.hc22000", essid=essid, bssid=bssid, type=htype)


# --- declarations are statically readable (the loader's hard rule) ----------
def test_declarations_are_ast_readable():
    decl = read_declarations(AGE_PY)["age"]   # raises if a declaration isn't a literal
    assert decl["target_api"] == 2
    assert "ui" in decl["capabilities"] and "web" in decl["capabilities"]
    assert "network" not in decl["capabilities"]   # local-only: no auth ack


# --- it loads through the real loader and reports loaded --------------------
def test_age_loads_through_the_loader(tmp_path):
    loader = Loader(
        config={"plugins": {"age": {"enabled": True,
                                    "data_path": str(tmp_path / "a.json")}}},
        capture=CaptureInterface(str(tmp_path)),
        connectivity=ConnectivityState(),
        services=ServiceRegistry(),
        search_paths=[os.path.normpath(PLUGIN_DIR)],
        logger=logging.getLogger("test.age"),
    )
    loaded = loader.load_all()
    names = {p.name: p for p in loaded}
    assert "age" in names, [p.name for p in loaded]
    assert names["age"].state == "loaded", names["age"].reason


# --- scoring by handshake TYPE (the substitute for enc-based scoring) -------
def test_points_by_handshake_type(tmp_path):
    p = _plugin(tmp_path, {"points_pmkid": 5, "points_eapol": 5,
                           "points_unknown": 1})
    p.on_handshake(_hs("pmkid"))
    p.on_handshake(_hs("eapol"))
    p.on_handshake(_hs("unknown"))
    assert p.network_points == 11          # 5 + 5 + 1
    assert p.handshake_count == 3
    assert p.total_handshakes_lifetime == 3


def test_unknown_type_falls_back(tmp_path):
    p = _plugin(tmp_path)
    p.on_handshake(_hs("something-weird"))
    assert p.network_points == 1           # scored as unknown


def test_streak_bonus_kicks_in_at_five(tmp_path):
    p = _plugin(tmp_path, {"points_eapol": 10})
    for _ in range(4):
        p.on_handshake(_hs("eapol"))
    assert p.network_points == 40          # no bonus yet
    p.on_handshake(_hs("eapol"))           # 5th -> x1.2
    assert p.network_points == 40 + int(10 * 1.2)
    assert p.streak == 5


# --- epochs/age, strength, personality -------------------------------------
def test_epochs_age_and_passive_strength(tmp_path):
    p = _plugin(tmp_path, {"passive_train_rate": 1.0})
    for _ in range(100):
        p.on_epoch(100, {})
    assert p.epochs == 100
    assert int(p.train_epochs) == 100
    assert p.get_age_title() == "Baby Steps"        # 100-epoch threshold
    assert p.get_strength_title() == "Sparring Novice"


def test_scholar_and_stealth_accrue(tmp_path):
    p = _plugin(tmp_path, {"stealth_quiet_epochs": 5})
    for _ in range(20):
        p.on_epoch(0, {})
    assert p.personality_points["scholar"] == 2      # +1 every 10
    assert p.personality_points["stealth"] >= 3      # quiet epochs
    assert p.get_dominant_personality() in ("Stealth", "Scholar")


def test_aggro_from_handshakes(tmp_path):
    p = _plugin(tmp_path, {"aggro_per_handshake": 3})
    p.on_handshake(_hs())
    p.on_handshake(_hs())
    assert p.personality_points["aggro"] == 6


# --- progress bar + titles -------------------------------------------------
def test_progress_bar_measures_from_previous_title(tmp_path):
    p = _plugin(tmp_path)
    p.epochs = 0
    assert p.get_progress_bar() == "[     ]"
    p.epochs = 50                                    # halfway to 100
    assert p.get_progress_bar() == "[==   ]"
    p.epochs = 111111                                # max title
    assert p.get_progress_bar() == "[MAX]"


def test_handshake_milestones_unlock(tmp_path):
    p = _plugin(tmp_path)
    p.on_handshake(_hs())
    assert "First Blood" in p.achievements


# --- decay -----------------------------------------------------------------
def test_decay_removes_points_after_inactivity(tmp_path):
    p = _plugin(tmp_path, {"decay_interval": 10, "decay_amount": 5,
                           "points_eapol": 100})
    p.on_handshake(_hs("eapol"))          # 100 pts at epoch 0
    assert p.network_points == 100
    for _ in range(25):                    # 25 idle epochs -> 2 intervals
        p.on_epoch(0, {})
    assert p.network_points == 90          # lost 2 * 5


# --- UI readout ------------------------------------------------------------
def test_ui_elements_register_and_update(tmp_path):
    p = _plugin(tmp_path, {"show_points": True, "show_progress": True,
                           "show_personality": True, "show_status": True})
    ui = StubUI().for_plugin("age")
    p.on_ui_setup(ui)
    p.on_handshake(_hs("eapol"))
    p.on_ui_update(ui)
    assert ui.get("Age") == "Unborn"
    assert ui.get("Points") == "5"
    assert ui.get("Progress") is not None


# --- persistence round-trip ------------------------------------------------
def test_state_persists_across_reload(tmp_path):
    p = _plugin(tmp_path, {"points_eapol": 7, "save_interval": 0})
    p.on_handshake(_hs("eapol"))
    for _ in range(5):
        p.on_epoch(0, {})
    p.on_unload()                          # force flush
    assert os.path.exists(p.data_path)
    data = json.load(open(p.data_path))
    assert data["points"] == 7 and data["epochs"] == 5

    p2 = _plugin(tmp_path, {"data_path": p.data_path})
    assert p2.network_points == 7
    assert p2.epochs == 5


def test_corrupt_state_file_is_quarantined_not_fatal(tmp_path):
    path = os.path.join(str(tmp_path), "age.json")
    open(path, "w").write("{ not json")
    p = _plugin(tmp_path, {"data_path": path})     # must not raise
    assert p.epochs == 0
    assert os.path.exists(path + ".corrupt")


# --- prestige / rebirth ----------------------------------------------------
def test_rebirth_resets_and_bumps_multiplier(tmp_path):
    p = _plugin(tmp_path, {"auto_rebirth": True, "prestige_bonus": 0.1,
                           "passive_train_rate": 0.0})
    # jump to the rebirth threshold
    p.epochs = max(p.age_titles)
    p.train_epochs = float(max(p.strength_titles))
    p.network_points = 999
    p._check_rebirth()                      # auto -> triggers
    assert p.prestige == 1
    assert abs(p.prestige_multiplier - 1.1) < 1e-9
    assert p.epochs == 0 and p.network_points == 0


def test_manual_rebirth_via_webhook(tmp_path):
    p = _plugin(tmp_path, {"auto_rebirth": False})
    p.epochs = max(p.age_titles)
    p.train_epochs = float(max(p.strength_titles))
    p._check_rebirth()                      # pending, not auto
    assert p.rebirth_pending is True
    # the rebirth button is offered only while pending + manual
    assert any(a["path"] == "rebirth" for a in p.webhook_actions())
    out = p.on_webhook("rebirth", None)
    assert isinstance(out, PluginRedirect)
    assert p.prestige == 1


# --- web page --------------------------------------------------------------
def test_stats_page_returns_a_pluginpage(tmp_path):
    p = _plugin(tmp_path)
    p.on_handshake(_hs("eapol"))
    out = p.on_webhook("view", None)
    assert isinstance(out, PluginPage)
    assert "Age" in out.body and "Network points" in out.body


def test_webhook_actions_hides_rebirth_until_pending(tmp_path):
    p = _plugin(tmp_path, {"auto_rebirth": False})
    assert all(a["path"] != "rebirth" for a in p.webhook_actions())


def test_on_ui_update_accepts_the_api2_state_arg(tmp_path):
    # The daemon calls on_ui_update(ui, state) at HOOK_API_VERSION 2; the hook
    # must accept the snapshot without a TypeError.
    from kestrel_pi.ui import UIState
    p = _plugin(tmp_path)
    ui = StubUI().for_plugin("age")
    p.on_ui_setup(ui)
    p.on_ui_update(ui, UIState())          # 3-arg form, as the daemon calls it
    assert ui.get("Age") == "Unborn"


def test_provides_age_status_service(tmp_path):
    from kestrel_pi.ui import UIState
    decl = read_declarations(AGE_PY)["age"]
    assert "age_status" in decl["provides"]
    p = _plugin(tmp_path, {"points_eapol": 10})
    for _ in range(5):                 # the 5th handshake queues a streak line
        p.on_handshake(_hs("eapol"))
    ui = StubUI().for_plugin("age")
    p.on_ui_setup(ui)
    p.on_ui_update(ui, UIState())      # rotation moves the line into _current_msg
    assert p.provide_age_status()      # non-empty
    assert isinstance(p.provide_age_status(), str)


def test_age_status_rotates_even_when_readout_hidden(tmp_path):
    # show_status off must not freeze the service the e-ink reads.
    p = _plugin(tmp_path, {"show_status": False})
    p.epochs = max(p.age_titles) - 1
    p.on_epoch(0, {})                  # crosses into the top title -> a message
    ui = StubUI().for_plugin("age")
    p.on_ui_setup(ui)
    p.on_ui_update(ui, None)
    assert p.provide_age_status() != "" or p._msg_queue == []
