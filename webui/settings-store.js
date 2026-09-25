import { createStore } from "/js/AlpineStore.js";
import * as API from "/js/api.js";
import { toastFrontendError, toastFrontendSuccess } from "/components/notifications/notification-store.js";

export const store = createStore("rrsiSettingsPrototype", {
    models: {}, loading: false, alive: true,
    images: [], imagesLoading: false, imageRequest: 0, imageAvailability: "loading",
    imageReason: null, imagesTruncated: false,
    imageGroups: [{key: "recommended", label: "Recommended"}, {key: "agent", label: "Agent Zero images"}, {key: "other", label: "Other local images"}],
    roles: [
        {key: "policy", label: "Agent", detail: "Main task execution"},
        {key: "utility", label: "Utility", detail: "Supporting model calls"},
        {key: "vision", label: "Vision", detail: "Image understanding"},
        {key: "proposer", label: "Proposer", detail: "Candidate improvements"},
        {key: "analyst", label: "Analyst", detail: "Evaluation analysis"},
        {key: "critic", label: "Critic", detail: "Candidate review"},
        {key: "digester", label: "Digester", detail: "Research summaries"},
        {key: "embedding", label: "Embedding", detail: "Vector representations"},
    ],
    automation: [
        {key: "automatic_capture", label: "Learn from future interactions", detail: "Only sanitized, reproducible tasks are eligible.", icon: "forum"},
        {key: "automatic_campaigns", label: "Run while idle", detail: "Check hourly; yield when foreground work resumes.", icon: "schedule"},
        {key: "automatic_activation", label: "Activate validated improvements", detail: "New conversations use the accepted version.", icon: "publish"},
    ],
    async loadModelPrices() {
        this.loading = true;
        try {
            const response = await API.callJsonApi("/plugins/rrsi/pricing", {});
            if (!this.alive) return;
            if (!response?.success) throw new Error(response?.error || "Model prices are unavailable.");
            this.models = response.data.roles;
        } catch (error) { if (this.alive) toastFrontendError(error.message, "RRSI pricing"); }
        finally { if (this.alive) this.loading = false; }
    },
    async loadImages() {
        const request = ++this.imageRequest;
        this.imagesLoading = true;
        try {
            const response = await API.callJsonApi("/plugins/rrsi/images", {});
            if (!this.alive || request !== this.imageRequest) return;
            if (!response?.success) throw new Error("Docker images could not be loaded. Try Refresh.");
            this.images = response.data.images;
            this.imageAvailability = response.data.availability;
            this.imageReason = response.data.reason;
            this.imagesTruncated = response.data.truncated;
        } catch (error) {
            if (this.alive && request === this.imageRequest) {
                this.imageAvailability = "unavailable";
                this.imageReason = "request_failed";
                toastFrontendError("Docker images could not be loaded. Try Refresh.", "RRSI images");
            }
        } finally { if (this.alive && request === this.imageRequest) this.imagesLoading = false; }
    },
    recommendedImage() { return this.images.find(image => image.recommended); },
    selectedImage(config) { return this.images.find(image => image.id === config.framework_image || image.tags.includes(config.framework_image)); },
    imageSelection(config) { return this.selectedImage(config)?.id || config.framework_image || ""; },
    groupedImages(key) { return this.images.filter(image => (image.recommended ? "recommended" : image.agent_zero ? "agent" : "other") === key); },
    imageSize(bytes) { return Number.isFinite(bytes) ? (bytes / 1e9).toFixed(bytes >= 1e9 ? 1 : 2) + " GB" : "Size unavailable"; },
    imageLabel(image) { return `${image.current_instance ? "Current Agent Zero image · " : ""}${image.recommended ? "Recommended · " : ""}${image.name} · ${this.imageSize(image.size_bytes)}${image.tags.length ? "" : " · " + image.id.slice(7, 19)}${image.selectable ? "" : " · Linux required"}`; },
    imageStatus() {
        if (this.imagesLoading) return "Checking locally available images…";
        if (this.imageAvailability === "empty") return "No local images found. Install an Agent Zero image in Docker Desktop, then Refresh.";
        if (this.imageAvailability === "unavailable") return this.imageReason === "docker_not_found"
            ? "The Docker command is unavailable to Agent Zero. Complete the controller’s Docker setup, then Refresh. Your selection is kept."
            : "Agent Zero could not reach Docker. Check that Docker Desktop is running and the controller has daemon access, then Refresh. Your selection is kept.";
        const recommended = this.recommendedImage();
        if (recommended?.current_instance) return "Recommended: the image used to create this running Agent Zero container. Its framework runtime check passed.";
        return recommended
            ? "Recommended: an official Agent Zero image that passed the runtime check. The current container’s image could not be verified."
            : "No recommendation passed the runtime check. Choose a Linux Agent Zero image; campaign setup will verify it.";
    },
    valid(price) { return price && [price.input_per_million, price.output_per_million].every(v => typeof v === "number" && Number.isFinite(v) && v >= 0); },
    priced(config) { return this.roles.filter(role => this.valid(config.pricing?.[role.key])).length; },
    available(config) { return this.roles.filter(role => this.valid(this.models[role.key]?.rates) && !config.pricing?.[role.key]).length; },
    incomplete(config) { return Object.values(config.pricing || {}).some(price => !this.valid(price)); },
    setRate(config, role, field, value) {
        config.pricing ||= {};
        const next = {...(config.pricing[role] || {input_per_million: null, output_per_million: null})};
        next[field] = value.trim() === "" ? null : Number(value);
        if (next.input_per_million === null && next.output_per_million === null) delete config.pricing[role];
        else config.pricing[role] = next;
    },
    clearRate(config, role) { delete config.pricing[role]; },
    applyDefaults(config) {
        config.pricing ||= {};
        let count = 0;
        for (const role of this.roles) {
            const rates = this.models[role.key]?.rates;
            if (!config.pricing[role.key] && this.valid(rates)) { config.pricing[role.key] = {...rates}; count++; }
        }
        toastFrontendSuccess(count ? `${count} available defaults added. Review the rates, then Save.` : "Existing rates were kept. No additional defaults are available.", "RRSI pricing");
    },
    resetCampaign(config) { Object.assign(config, {rounds: 20, candidates: 2, trials_per_task: 2, baseline_evaluations: 3}); },
});
