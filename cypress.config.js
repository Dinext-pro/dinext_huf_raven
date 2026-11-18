const { spawn } = require("node:child_process");
const path = require("node:path");
const readline = require("node:readline");

// Keep fixture creation and cleanup in one local Python context. Closing the
// pipe releases it even if Cypress aborts before its after:run callback.
function fixtureProcess(bench) {
    const child = spawn(path.join(bench, "env/bin/python"), ["-m", "dinext_huf_raven.tests.acceptance"], {
        cwd: path.join(bench, "sites"),
        stdio: ["pipe", "pipe", "inherit"],
    });
    const pending = [];
    const lines = readline.createInterface({ input: child.stdout });
    lines.on("line", (line) => {
        if (!line.startsWith("DINEXT_QA ")) return;
        const response = JSON.parse(line.slice("DINEXT_QA ".length));
        const waiter = pending.shift();
        if (response.error) waiter.reject(new Error(response.error));
        else waiter.resolve(response.result);
    });
    const ended = new Promise((resolve, reject) => {
        const failed = (error) => {
            for (const waiter of pending.splice(0)) waiter.reject(error);
            reject(error);
        };
        child.on("error", failed);
        child.on("exit", (code) => code === 0 ? resolve() : failed(new Error("Local Raven QA fixture exited with " + code)));
    });
    // A setup error can happen before Cypress has called either task.
    ended.catch(() => {});
    return {
        request(message) {
            if (child.exitCode !== null) return Promise.reject(new Error("Local QA fixture is no longer running."));
            return new Promise((resolve, reject) => {
                pending.push({ resolve, reject });
                child.stdin.write(JSON.stringify(message) + "\n");
            });
        },
        close() {
            child.stdin.end();
            return ended;
        },
    };
}

module.exports = {
    video: false,
    includeShadowDom: true,
    defaultCommandTimeout: 60000,
    requestTimeout: 120000,
    responseTimeout: 120000,
    pageLoadTimeout: 120000,
    taskTimeout: 150000,
    viewportWidth: 1440,
    viewportHeight: 960,
    e2e: {
        specPattern: "cypress/integration/**/*.js",
        setupNodeEvents(on, config) {
            const base = new URL(config.baseUrl || "http://localhost:8000");
            if (!["localhost", "127.0.0.1"].includes(base.hostname)) {
                throw new Error("The assistant acceptance suite requires the localhost site.");
            }
            let fixture;
            const bench = process.env.DINEXT_AI_BENCH || path.resolve(__dirname, "../..");
            on("task", {
                assistantProgress(message) {
                    const safe = String(message).replace(/\S+@example\.invalid/gi, "[employee]")
                        .replace(/\b[\da-f]{20,}\b/gi, "[fixture]");
                    console.log("Raven QA:", safe.slice(0, 300));
                    return null;
                },
                assistantFixture() {
                    if (!fixture) fixture = fixtureProcess(bench);
                    return fixture.request({ command: "fixture" });
                },
                assistantLogin(index) {
                    return fixture.request({ command: "login", index });
                },
                assistantReceipt(request) {
                    return fixture.request({ ...request, command: "receipt" });
                },
            });
            on("after:run", () => fixture ? fixture.close() : undefined);
            return config;
        },
    },
};
