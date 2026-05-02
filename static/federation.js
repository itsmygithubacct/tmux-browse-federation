// Federation Config subsection — discovered peers list, with the
// pair-request / accept / decline / unpair actions wired to
// /api/peers/* on the local server.
//
// The pairing model is "request → accept on both sides" similar to
// how Connected Endpoints does Share Config: one side initiates,
// the other side sees a pending request, the operator decides. No
// peer's sessions appear in the dashboard until both sides have
// explicitly accepted.

const _FED_REFRESH_INTERVAL_MS = 5000;

async function loadFederationPeers() {
    const wrap = document.getElementById("federation-wrap");
    if (!wrap) return;  // happens during early init before the DOM exists
    const list = document.getElementById("federation-list");
    const count = document.getElementById("federation-count");
    if (!list || !count) return;

    const r = await api("GET", "/api/peers");
    const peers = (r && r.peers) || [];

    // Count: paired + pending. Discovered-but-untouched peers don't
    // bump the badge — only actionable items.
    const actionable = peers.filter((p) =>
        p.status === "paired" || p.status === "request-pending"
        || p.status === "request-sent");
    count.textContent = String(actionable.length);

    list.textContent = "";
    if (!peers.length) {
        list.append(el("div", { class: "dim" },
            "No peers visible. Run tmux-browse on another host on this " +
            "LAN to discover it."));
        return;
    }
    for (const p of peers) {
        list.append(_buildFederationRow(p));
    }
}

function _buildFederationRow(p) {
    const row = el("div", {
        class: "ext-card federation-row",
        "data-peer-id": p.device_id,
    });
    const head = el("div", { class: "ext-card-head" },
        el("span", { class: "ext-card-label" }, p.hostname || "(unknown)"),
        _statusBadge(p.status, p.online),
    );
    row.append(head);

    const meta = [];
    if (p.url) meta.push(p.url);
    if (p.version) meta.push(`v${p.version}`);
    if (!p.online) meta.push("offline");
    if (meta.length) {
        row.append(el("div", { class: "ext-card-desc" }, meta.join(" · ")));
    }

    const actions = el("div", { class: "ext-card-actions" });
    if (p.status === "request-pending") {
        actions.append(
            el("button", {
                class: "btn green",
                onclick: () => _acceptPair(p.device_id, p.hostname),
            }, "Accept"),
            el("button", {
                class: "btn",
                onclick: () => _declinePair(p.device_id, p.hostname),
            }, "Decline"),
        );
        const note = el("span", { class: "dim", style: "font-size:0.78rem" },
            `${p.hostname || "peer"} wants to pair`);
        actions.append(note);
    } else if (p.status === "paired") {
        actions.append(el("button", {
            class: "btn red",
            onclick: () => _unpair(p.device_id, p.hostname),
        }, "Unpair"));
    } else if (p.status === "request-sent") {
        actions.append(el("span", { class: "dim", style: "font-size:0.82rem" },
            "request sent · waiting for accept"));
    } else {
        // discovered, no activity yet
        actions.append(el("button", {
            class: "btn blue",
            onclick: () => _requestPair(p.device_id, p.hostname),
            disabled: !p.online ? "disabled" : null,
        }, "Request Pair"));
    }
    row.append(actions);
    return row;
}

function _statusBadge(status, online) {
    if (status === "paired" && online) {
        return el("span", { class: "ext-card-state enabled" }, "paired");
    }
    if (status === "paired") {
        return el("span", { class: "ext-card-state" }, "paired (offline)");
    }
    if (status === "request-pending") {
        return el("span", { class: "ext-card-state",
            style: "color:var(--yellow);border-color:var(--yellow)" },
            "pair request");
    }
    if (status === "request-sent") {
        return el("span", { class: "ext-card-state",
            style: "color:var(--blue);border-color:var(--blue)" },
            "waiting");
    }
    return el("span", { class: "ext-card-state" }, "discovered");
}

async function _requestPair(deviceId, hostname) {
    const status = document.getElementById("federation-status");
    if (status) {
        status.textContent = `sending pair request to ${hostname || deviceId}…`;
        status.className = "dim";
    }
    const r = await api("POST", "/api/peers/pair-request-out",
                         { device_id: deviceId });
    if (r.ok) {
        if (status) status.textContent = `request sent to ${hostname || deviceId}`;
    } else if (status) {
        status.textContent = "error: " + (r.error || "unknown");
        status.className = "err";
    }
    await loadFederationPeers();
}

async function _acceptPair(deviceId, hostname) {
    const status = document.getElementById("federation-status");
    if (status) {
        status.textContent = `accepting pair request from ${hostname || deviceId}…`;
        status.className = "dim";
    }
    const r = await api("POST", "/api/peers/pair-accept",
                         { device_id: deviceId });
    if (r.ok) {
        if (status) {
            status.textContent = `paired with ${hostname || deviceId}`;
            status.className = "ok";
        }
    } else if (status) {
        status.textContent = "error: " + (r.error || "unknown");
        status.className = "err";
    }
    await loadFederationPeers();
}

async function _declinePair(deviceId, hostname) {
    const r = await api("POST", "/api/peers/pair-decline",
                         { device_id: deviceId });
    const status = document.getElementById("federation-status");
    if (status) {
        status.textContent = r.ok
            ? `declined ${hostname || deviceId}`
            : ("error: " + (r.error || "unknown"));
        status.className = r.ok ? "dim" : "err";
    }
    await loadFederationPeers();
}

async function _unpair(deviceId, hostname) {
    if (!confirm(`Unpair from ${hostname || deviceId}? Their sessions will stop appearing in your dashboard.`)) return;
    const r = await api("POST", "/api/peers/unpair",
                         { device_id: deviceId });
    const status = document.getElementById("federation-status");
    if (status) {
        status.textContent = r.ok
            ? `unpaired from ${hostname || deviceId}`
            : ("error: " + (r.error || "unknown"));
        status.className = r.ok ? "dim" : "err";
    }
    await loadFederationPeers();
}

function startFederationPoll() {
    loadFederationPeers().catch(() => {});
    setInterval(() => loadFederationPeers().catch(() => {}),
                _FED_REFRESH_INTERVAL_MS);
}
