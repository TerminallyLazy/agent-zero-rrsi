import { createStore } from "/js/AlpineStore.js";
import * as API from "/js/api.js";
import { store as notifications } from "/components/notifications/notification-store.js";

const endpoint = "/plugins/rrsi/rrsi";
export const store = createStore("rrsiDashboard", {
    state: null, tasks: [], versions: [], selected: "", busy: false,
    evidence: "", evidenceKind: "summary", round: 0, variant: "A", polling: null, mountGeneration: 0,
    async call(action, extra = {}) {
        const response = await API.callJsonApi(endpoint, { action, ...(this.selected ? { campaign_id: this.selected } : {}), ...extra });
        if (!response?.success) throw new Error(response?.error || "RRSI request failed");
        return response.data;
    },
    toast(text, type = "info") { notifications.addFrontendToastOnly(type, text, "", 6); },
    async refresh() {
        try {
            this.state = await this.call("status");
            if (!this.selected && this.state.campaign) this.selected = this.state.campaign.id;
            this.tasks = (await this.call("tasks")).tasks;
            this.versions = (await this.call("versions")).versions;
        } catch (error) { this.toast(error.message, "error"); }
    },
    async action(action) {
        this.busy = true;
        try {
            const result = await this.call(action, action === "start" ? { campaign_id: null } : {});
            if (result.campaign_id) this.selected = result.campaign_id;
            this.toast(action === "setup" ? "Environment checks passed" : "RRSI request recorded");
            await this.refresh();
        } catch (error) { this.toast(error.message, "error"); }
        finally { this.busy = false; }
    },
    async inspect() {
        try {
            const result = await this.call("evidence", { kind: this.evidenceKind, t: Number(this.round), variant: this.variant });
            this.evidence = typeof result.data === "string" ? result.data : JSON.stringify(result.data, null, 2);
        } catch (error) { this.toast(error.message, "error"); }
    },
    async mount() {
        const generation = ++this.mountGeneration;
        await this.refresh();
        if (generation !== this.mountGeneration) return;
        if (!this.polling) this.polling = setInterval(() => this.refresh(), 5000);
    },
    unmount() { ++this.mountGeneration; clearInterval(this.polling); this.polling = null; },
    money(value) { return typeof value === "number" ? `$${value.toFixed(4)}` : "Unavailable"; },
});
