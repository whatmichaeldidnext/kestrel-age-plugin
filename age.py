"""age - progression/gamification readout for kestrel, ported from AlienMajik's
pwnagotchi `age` plugin (v5.2.0, MIT).

The device "ages" with epochs and grows "stronger" over time, earns points for
handshakes, levels through named titles with a bit of lore, develops a dominant
personality, and can prestige (rebirth) for a point multiplier. It contributes a
dashboard readout and a web stats page, and persists its progress atomically.

Ported to the kestrel plugin API (docs/developer/plugin-development.md). The hook
bodies are faithful to the original; three features could not be carried over
because kestrel's architecture doesn't expose what they relied on, and each has
an honest substitute:

* **Scoring is by handshake type, not encryption suite.** The original scored
  WPA3/WPA2/WPA/WEP/open from bettercap's per-AP ``encryption`` field. kestrel's
  ``HandshakeEvent`` exposes ``type`` (``pmkid``/``eapol``/``unknown``) only, so
  ``points_map`` is keyed on that instead. (The enc-based "Crypto King" secret
  achievement is dropped with it.)
* **Strength accrues passively per epoch.** The original grew strength from
  ``on_ai_training_step`` (bettercap's on-board AI). kestrel has no such hook —
  the small board never trains on-device and remote-train steps aren't dispatched
  to plugins — so strength tracks the plugin's ``passive_train_rate`` fallback.
* **No deauth/association counters.** kestrel dispatches no such events, so those
  counters and the aggro-from-deauth are gone; aggro comes from handshakes.

Not yet ported (pure bookkeeping, easy to add on request): random events
(time-warp/windfall/personality-swap) and the time/enc secret achievements.

Local-only: it reads events and writes its own JSON state, nothing leaves the
device, so no ``network`` capability and no authorized-use ack.
"""

import html
import json
import os
import random
import threading
import time

from kestrel_pi import Plugin


class Age(Plugin):
    __author__ = "AlienMajik (ported)"
    __version__ = "5.2.0-kestrel1"
    __license__ = "MIT"
    __description__ = ("Age/strength progression, points, titles, personality, "
                       "prestige and a web stats page (ported from pwnagotchi).")

    __target_api__ = 2
    __capabilities__ = ("ui", "web")
    # The e-ink face plugin (optionally) shows this rotating status line.
    __provides__ = ("age_status",)
    __options__ = {
        # Points per handshake TYPE (kestrel exposes type, not encryption).
        "points_pmkid": {"type": "int", "default": 5},
        "points_eapol": {"type": "int", "default": 5},
        "points_unknown": {"type": "int", "default": 1},
        # Progression / decay knobs (same meaning as upstream).
        "passive_train_rate": {"type": "float", "default": 0.1},
        "decay_interval": {"type": "int", "default": 50},
        "decay_amount": {"type": "int", "default": 10},
        "stealth_quiet_epochs": {"type": "int", "default": 5},
        "aggro_per_handshake": {"type": "int", "default": 3},
        "repeat_ap_penalty": {"type": "bool", "default": False},
        "save_interval": {"type": "int", "default": 60},
        "max_status_len": {"type": "int", "default": 40},
        "prestige_bonus": {"type": "float", "default": 0.1},
        "auto_rebirth": {"type": "bool", "default": True},
        # Readout visibility.
        "show_points": {"type": "bool", "default": True},
        "show_progress": {"type": "bool", "default": True},
        "show_personality": {"type": "bool", "default": False},
        "show_status": {"type": "bool", "default": True},
        # Where to persist. Empty -> <capture-store>/age/age_strength.json.
        "data_path": {"type": "str", "default": ""},
    }
    # A GET stats page; the rebirth button appears only when a rebirth is
    # pending and auto_rebirth is off (see webhook_actions).
    __actions__ = (
        {"label": "Stats", "path": "view", "method": "get", "order": 1},
        {"label": "Rebirth", "path": "rebirth", "method": "post", "order": 2},
    )

    # --- static content (titles + a little lore) ---------------------------
    DEFAULT_AGE_TITLES = {
        100: "Baby Steps", 500: "Getting the Hang of It", 1000: "Neon Spawn",
        2000: "Script Kiddie", 5000: "WiFi Outlaw", 10000: "Data Raider",
        25000: "Prophet", 33333: "Off the Grid", 55555: "Multiversed",
        111111: "Intergalactic",
    }
    DEFAULT_STRENGTH_TITLES = {
        100: "Sparring Novice", 300: "Gear Tickler", 500: "Fleshbag",
        1500: "Lightweight", 2000: "Deauth King", 2500: "Handshake Hunter",
        3333: "Unstoppable", 55555: "Rev-9", 111111: "Kuato",
    }
    HANDSHAKE_MILESTONES = (
        (1, "First Blood"), (10, "Double Digits"), (100, "Century Mark"),
        (1000, "Thousand Claps"), (5000, "Legend"),
    )
    QUOTES = {
        "success": ["Groovy.", "Come get some.", "This is my BOOMSTICK!",
                    "Hail to the king, baby."],
        "warning": ["It's just a flesh wound!", "I'm not dead!", "Run away!"],
        "random": ["Ni!", "Bring out your dead!", "Shop smart. Shop S-Mart."],
    }
    LORE = {
        "age": {
            "Baby Steps": "A newborn spark in the digital void.",
            "Getting the Hang of It": "Patterns emerge from the noise.",
            "Neon Spawn": "Born in the glow of stray signals.",
            "Script Kiddie": "Borrowed tools, own hunger.",
            "WiFi Outlaw": "Feared by routers everywhere.",
            "Data Raider": "Each handshake a trophy.",
            "Prophet": "The future whispers in your ear.",
            "Off the Grid": "The grid cannot contain you.",
            "Multiversed": "Every frequency, yours to explore.",
            "Intergalactic": "Your legend echoes across the cosmos.",
        },
        "strength": {
            "Sparring Novice": "You spar with the basics.",
            "Deauth King": "The airwaves bend to your will.",
            "Handshake Hunter": "No four-way handshake is safe.",
            "Unstoppable": "Nothing on the band can stop you now.",
        },
    }

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def on_loaded(self):
        self.log = self.context.logger
        # RLock, not Lock: some helpers that take the lock are called from other
        # locked sections (as in the original), so a plain Lock would deadlock.
        self._lock = threading.RLock()
        self._setup()
        self._load_data()
        self.log.info("[age] loaded (epochs=%d, points=%d)",
                      self.epochs, self.network_points)

    def _setup(self):
        o = self.context.options
        self.points_map = {
            "pmkid": int(o.get("points_pmkid", 5)),
            "eapol": int(o.get("points_eapol", 5)),
            "unknown": int(o.get("points_unknown", 1)),
        }
        self.passive_train_rate = max(0.0, float(o.get("passive_train_rate", 0.1)))
        self.decay_interval = max(0, int(o.get("decay_interval", 50)))
        self.decay_amount = max(0, int(o.get("decay_amount", 10)))
        self.stealth_quiet_epochs = max(1, int(o.get("stealth_quiet_epochs", 5)))
        self.aggro_per_handshake = max(0, int(o.get("aggro_per_handshake", 3)))
        self.repeat_ap_penalty = bool(o.get("repeat_ap_penalty", False))
        self.save_interval = max(0, int(o.get("save_interval", 60)))
        self.max_status_len = max(10, int(o.get("max_status_len", 40)))
        self.prestige_bonus = max(0.0, float(o.get("prestige_bonus", 0.1)))
        self.auto_rebirth = bool(o.get("auto_rebirth", True))
        self.show_points = bool(o.get("show_points", True))
        self.show_progress = bool(o.get("show_progress", True))
        self.show_personality = bool(o.get("show_personality", False))
        self.show_status = bool(o.get("show_status", True))

        data_path = str(o.get("data_path", "") or "")
        if not data_path:
            base = getattr(self.context.capture, "store_dir", ".")
            data_path = os.path.join(base, "age", "age_strength.json")
        self.data_path = data_path

        self.age_titles = dict(self.DEFAULT_AGE_TITLES)
        self.strength_titles = dict(self.DEFAULT_STRENGTH_TITLES)

        # progression state
        self.epochs = 0
        self.train_epochs = 0.0
        self.network_points = 0
        self.handshake_count = 0            # this run
        self.total_handshakes_lifetime = 0
        self.last_active_epoch = 0
        self.streak = 0
        self.quiet_epochs = 0
        self.handshake_this_epoch = False
        self.personality_points = {"aggro": 0, "stealth": 0, "scholar": 0}
        self.prestige = 0
        self.prestige_multiplier = 1.0
        self.rebirth_pending = False
        self.seen_aps = {}
        self.achievements = set()
        self.prev_age_title = None
        self.prev_strength_title = None

        # status rotation
        self._msg_queue = []
        self._current_msg = ""
        self._hold_left = 0
        # save throttle
        self._last_save = 0.0

    def on_config_changed(self, config):
        # Re-read the simple knobs live; titles/paths stay as loaded.
        self._setup_live_from(config)
        return True

    def _setup_live_from(self, config):
        # Only the render/knob options are safe to swap live; keep state intact.
        for key, attr, cast in (
            ("passive_train_rate", "passive_train_rate", float),
            ("decay_interval", "decay_interval", int),
            ("decay_amount", "decay_amount", int),
            ("save_interval", "save_interval", int),
            ("show_points", "show_points", bool),
            ("show_progress", "show_progress", bool),
            ("show_personality", "show_personality", bool),
            ("show_status", "show_status", bool),
            ("auto_rebirth", "auto_rebirth", bool),
        ):
            if key in config:
                try:
                    setattr(self, attr, cast(config[key]))
                except (TypeError, ValueError):
                    pass

    def on_unload(self):
        self._save(force=True)

    # ------------------------------------------------------------------ #
    # events
    # ------------------------------------------------------------------ #
    def on_epoch(self, epoch, info):
        with self._lock:
            self.epochs += 1
            # Strength: passive accrual (no on_ai_training_step in kestrel).
            self.train_epochs += self.passive_train_rate
            if self.epochs % 10 == 0:
                self.personality_points["scholar"] += 1
            # Stealth rewards sustained quiet, not every idle epoch.
            if self.handshake_this_epoch:
                self.quiet_epochs = 0
            else:
                self.quiet_epochs += 1
                if self.quiet_epochs % self.stealth_quiet_epochs == 0:
                    self.personality_points["stealth"] += 1
            self.handshake_this_epoch = False
        self._apply_decay()
        self._check_titles()
        self._check_rebirth()
        self._save()

    def on_handshake(self, event):
        enc = getattr(event, "type", "") or "unknown"
        if enc not in self.points_map:
            enc = "unknown"
        bssid = (getattr(event, "bssid", "") or "").lower()
        essid = getattr(event, "essid", "") or "unknown"
        with self._lock:
            points = self.points_map.get(enc, 1) * self.prestige_multiplier
            points *= self._repeat_factor(bssid)
            self.streak += 1
            if self.streak >= 5:
                points *= 1.2
                if self.streak == 5 or self.streak % 10 == 0:
                    self._push(self._flavored("Streak x%d! +20%%" % self.streak,
                                              "success", 0.5))
            points = int(points)
            self.network_points += points
            self.handshake_count += 1
            self.total_handshakes_lifetime += 1
            self.last_active_epoch = self.epochs
            self.personality_points["aggro"] += self.aggro_per_handshake
            self.handshake_this_epoch = True
            self.quiet_epochs = 0
            self._check_milestones()
        self.log.info("[age] handshake %s (%s) +%d pts, streak %d",
                      essid, enc, points, self.streak)
        self._save()

    # ------------------------------------------------------------------ #
    # progression helpers
    # ------------------------------------------------------------------ #
    def _repeat_factor(self, bssid):
        if not self.repeat_ap_penalty or not bssid:
            return 1.0
        seen = self.seen_aps.get(bssid, 0)
        self.seen_aps[bssid] = seen + 1
        if len(self.seen_aps) > 4000:
            self.seen_aps = dict(list(self.seen_aps.items())[-2000:])
        return max(0.25, 1.0 / (1 + seen))

    def _title_for(self, titles, value, base_default):
        for t in sorted(titles, reverse=True):
            if value >= t:
                base = titles[t]
                return "Reborn %s" % base if self.prestige > 0 else base
        return base_default

    def get_age_title(self):
        if self.rebirth_pending:
            return "Ready for Rebirth"
        return self._title_for(self.age_titles, self.epochs, "Unborn")

    def get_strength_title(self):
        return self._title_for(self.strength_titles, int(self.train_epochs),
                               "Untrained")

    def _next_age_threshold(self):
        for t in sorted(self.age_titles):
            if self.epochs < t:
                return t
        return None

    def _prev_age_threshold(self):
        prev = 0
        for t in sorted(self.age_titles):
            if self.epochs >= t:
                prev = t
            else:
                break
        return prev

    def get_progress_bar(self):
        nxt = self._next_age_threshold()
        if nxt is None:
            return "[MAX]"
        prev = self._prev_age_threshold()
        span = max(1, nxt - prev)
        progress = min(1.0, max(0.0, (self.epochs - prev) / float(span)))
        n = 5
        filled = min(n, int(progress * n))
        if progress > 0.8:
            return "[" + ">" * filled + "~" * (n - filled) + "]"
        return "[" + "=" * filled + " " * (n - filled) + "]"

    def get_dominant_personality(self):
        if not any(self.personality_points.values()):
            return "Neutral"
        return max(self.personality_points,
                   key=self.personality_points.get).capitalize()

    def _check_titles(self):
        with self._lock:
            if self.rebirth_pending:
                return
            at = self.get_age_title()
            if at != self.prev_age_title:
                if self.prev_age_title is not None:
                    lore = self.LORE["age"].get(at.replace("Reborn ", ""))
                    self._push(self._flavored(lore or "%s achieved!" % at,
                                              "success", 0.3))
                self.prev_age_title = at
            st = self.get_strength_title()
            if st != self.prev_strength_title:
                if self.prev_strength_title is not None:
                    lore = self.LORE["strength"].get(st.replace("Reborn ", ""))
                    self._push(self._flavored(lore or "Evolved to %s!" % st,
                                              "success", 0.3))
                self.prev_strength_title = st

    def _check_milestones(self):
        for n, name in self.HANDSHAKE_MILESTONES:
            if self.total_handshakes_lifetime >= n and name not in self.achievements:
                self.achievements.add(name)
                self._push(self._flavored("Achievement: %s" % name, "success", 0.5))

    def _apply_decay(self):
        if self.decay_interval <= 0 or self.decay_amount <= 0:
            return
        with self._lock:
            inactive = self.epochs - self.last_active_epoch
            if inactive < self.decay_interval:
                return
            intervals = inactive // self.decay_interval
            self.last_active_epoch += intervals * self.decay_interval
            if self.network_points <= 0:
                return
            lost = min(self.network_points, int(intervals * self.decay_amount))
            if lost <= 0:
                return
            self.network_points -= lost
            self.streak = 0
            self._push(self._flavored("Decayed by %d points." % lost, "warning", 0.5))
            self.log.info("[age] decay: lost %d points", lost)
        self._save(force=True)

    # --- prestige ---
    def _rebirth_ready(self):
        return (self.epochs >= max(self.age_titles) and
                int(self.train_epochs) >= max(self.strength_titles))

    def _check_rebirth(self):
        with self._lock:
            if self.rebirth_pending:
                if self.auto_rebirth:
                    self._do_rebirth()
                return
            if self._rebirth_ready():
                if self.auto_rebirth:
                    self._do_rebirth()
                else:
                    self.rebirth_pending = True
                    self._push(self._flavored(
                        "Rebirth available - you will transcend.", "warning"))
                    self.log.info("[age] rebirth conditions met")

    def _do_rebirth(self):
        with self._lock:
            self.prestige += 1
            self.prestige_multiplier = 1.0 + (self.prestige * self.prestige_bonus)
            self.epochs = 0
            self.train_epochs = 0.0
            self.network_points = 0
            self.handshake_count = 0
            self.last_active_epoch = 0
            self.streak = 0
            self.quiet_epochs = 0
            self.seen_aps = {}
            self.personality_points = {"aggro": 0, "stealth": 0, "scholar": 0}
            self.rebirth_pending = False
            self.prev_age_title = None
            self.prev_strength_title = None
            self._push("Rebirth #%d! Multiplier now %.1fx" %
                       (self.prestige, self.prestige_multiplier))
            self.log.info("[age] rebirth #%d, x%.2f", self.prestige,
                          self.prestige_multiplier)
        self._save(force=True)

    # ------------------------------------------------------------------ #
    # text / status
    # ------------------------------------------------------------------ #
    @staticmethod
    def abrev_number(num):
        try:
            num = float(num)
        except (TypeError, ValueError):
            return "0"
        sign = "-" if num < 0 else ""
        num = abs(num)
        for unit in ("", "K", "M", "B"):
            if num < 1000:
                if unit == "" and float(num).is_integer():
                    return "%s%d" % (sign, int(num))
                txt = "%.1f" % num
                if txt.endswith(".0"):
                    txt = txt[:-2]
                return "%s%s%s" % (sign, txt, unit)
            num /= 1000.0
        txt = "%.1f" % num
        return "%s%sT" % (sign, txt[:-2] if txt.endswith(".0") else txt)

    def _shorten(self, text):
        text = " ".join(str(text or "").split())
        if len(text) <= self.max_status_len:
            return text
        return text[:max(1, self.max_status_len - 1)].rstrip() + "…"

    def _quote(self, cat):
        pool = self.QUOTES.get(cat)
        return random.choice(pool) if pool else ""

    def _flavored(self, base, cat=None, chance=1.0):
        q = self._quote(cat) if cat and random.random() < chance else ""
        return ("%s %s" % (q, base)).strip() if q else base

    def _push(self, text):
        text = self._shorten(text)
        if text:
            self._msg_queue.append(text)

    def _rotate_status(self):
        if self._hold_left > 0:
            self._hold_left -= 1
            return
        if self._msg_queue:
            self._current_msg = self._msg_queue.pop(0)
            self._hold_left = 2

    def provide_age_status(self):
        """The current rotating status line, for another plugin (the e-ink face)
        to display. Rotation itself is driven by on_ui_update; this just reports
        what is current. Returns "" when there is nothing to say."""
        with self._lock:
            return self._current_msg or ""

    # ------------------------------------------------------------------ #
    # UI readout
    # ------------------------------------------------------------------ #
    def on_ui_setup(self, ui):
        ui.add_element("Age", "Unborn", label="Age")
        ui.add_element("Strength", "Untrained", label="Strength")
        if self.show_points:
            ui.add_element("Points", "0", label="Points", kind="number")
        if self.show_progress:
            ui.add_element("Progress", "[     ]", label="Progress")
        if self.show_personality:
            ui.add_element("Personality", "Neutral", label="Trait")
        if self.show_status:
            ui.add_element("AgeStatus", "", label="Age")

    def on_ui_update(self, ui, state=None):
        with self._lock:
            ui.set("Age", self.get_age_title())
            ui.set("Strength", self.get_strength_title())
            if self.show_points:
                ui.set("Points", self.abrev_number(self.network_points))
            if self.show_progress:
                ui.set("Progress", self.get_progress_bar())
            if self.show_personality:
                ui.set("Personality", self.get_dominant_personality())
            # Rotate regardless of show_status so provide_age_status stays live
            # for the e-ink face even when this dashboard element is hidden.
            self._rotate_status()
            if self.show_status:
                ui.set("AgeStatus", self._current_msg)

    # ------------------------------------------------------------------ #
    # web page / row buttons
    # ------------------------------------------------------------------ #
    def webhook_actions(self):
        acts = [a for a in self.__actions__ if a["path"] == "view"]
        if self.rebirth_pending and not self.auto_rebirth:
            acts += [a for a in self.__actions__ if a["path"] == "rebirth"]
        return tuple(acts)

    def on_webhook(self, path, request):
        if path == "rebirth":
            if not self.rebirth_pending:
                return self.context.redirect("No rebirth pending.", error=True)
            self._do_rebirth()
            return self.context.redirect("Rebirth #%d triggered." % self.prestige)
        if path in (None, "", "view", "index"):
            return self.context.page("Age - kestrel", self._stats_html())
        return self.context.redirect("No such view.", error=True)

    def _stats_html(self):
        esc = self.context.esc
        with self._lock:
            rows = [
                ("Age title", self.get_age_title()),
                ("Epochs", "%d" % self.epochs),
                ("Strength title", self.get_strength_title()),
                ("Train epochs", "%d" % int(self.train_epochs)),
                ("Network points", "%d" % self.network_points),
                ("Prestige", "%d (%.1fx)" % (self.prestige, self.prestige_multiplier)),
                ("Handshakes (run)", "%d" % self.handshake_count),
                ("Handshakes (lifetime)", "%d" % self.total_handshakes_lifetime),
                ("Current streak", "%d" % self.streak),
                ("Dominant trait", self.get_dominant_personality()),
                ("Aggro / Stealth / Scholar", "%(aggro)d / %(stealth)d / %(scholar)d"
                 % self.personality_points),
                ("Achievements", ", ".join(sorted(self.achievements)) or "—"),
            ]
        body = "".join("<tr><th>%s</th><td>%s</td></tr>"
                       % (esc(str(k)), esc(str(v))) for k, v in rows)
        return "<h1>Age</h1><table>%s</table>" % body

    # ------------------------------------------------------------------ #
    # persistence (atomic + throttled)
    # ------------------------------------------------------------------ #
    def _load_data(self):
        try:
            with open(self.data_path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            self.log.error("[age] stats unreadable (%s) - starting fresh", e)
            try:
                os.replace(self.data_path, self.data_path + ".corrupt")
            except OSError:
                pass
            return
        with self._lock:
            self.epochs = int(data.get("epochs", 0))
            self.train_epochs = float(data.get("train_epochs", 0))
            self.network_points = int(data.get("points", 0))
            self.handshake_count = int(data.get("handshakes", 0))
            self.total_handshakes_lifetime = int(
                data.get("total_handshakes", self.handshake_count))
            self.last_active_epoch = int(data.get("last_active", 0))
            self.streak = int(data.get("streak", 0))
            self.quiet_epochs = int(data.get("quiet_epochs", 0))
            for t in ("aggro", "stealth", "scholar"):
                self.personality_points[t] = int(data.get("personality_%s" % t, 0))
            self.prestige = int(data.get("prestige", 0))
            self.prestige_multiplier = float(
                data.get("prestige_multiplier",
                         1.0 + self.prestige * self.prestige_bonus))
            self.rebirth_pending = bool(data.get("rebirth_pending", False))
            self.achievements = set(data.get("achievements", []))
            self.seen_aps = {str(k): int(v)
                             for k, v in (data.get("seen_aps") or {}).items()}
            self.prev_age_title = data.get("prev_age") or self.get_age_title()
            self.prev_strength_title = (data.get("prev_strength")
                                        or self.get_strength_title())

    def _save(self, force=False):
        now = time.time()
        with self._lock:
            if (not force and self.save_interval
                    and (now - self._last_save) < self.save_interval):
                return
            self._last_save = now
            data = {
                "epochs": self.epochs,
                "train_epochs": round(self.train_epochs, 3),
                "points": self.network_points,
                "handshakes": self.handshake_count,
                "total_handshakes": self.total_handshakes_lifetime,
                "last_active": self.last_active_epoch,
                "streak": self.streak,
                "quiet_epochs": self.quiet_epochs,
                "personality_aggro": self.personality_points["aggro"],
                "personality_stealth": self.personality_points["stealth"],
                "personality_scholar": self.personality_points["scholar"],
                "prestige": self.prestige,
                "prestige_multiplier": self.prestige_multiplier,
                "rebirth_pending": self.rebirth_pending,
                "achievements": sorted(self.achievements),
                "seen_aps": self.seen_aps if self.repeat_ap_penalty else {},
                "prev_age": self.prev_age_title,
                "prev_strength": self.prev_strength_title,
                "version": self.__version__,
            }
        try:
            os.makedirs(os.path.dirname(self.data_path), exist_ok=True)
            tmp = self.data_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.data_path)
        except OSError as e:
            self.log.error("[age] save error: %s", e)
            try:
                os.remove(self.data_path + ".tmp")
            except OSError:
                pass
