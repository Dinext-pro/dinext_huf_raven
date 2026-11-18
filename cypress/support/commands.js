Cypress.Commands.add("login", (index) => {
    return cy.session(["Raven employee", index], () => {
        cy.task("assistantLogin", index, { log: false }).then(({ user, cookies }) => {
            for (const [name, value] of Object.entries(cookies)) {
                cy.setCookie(name, value, { httpOnly: name === "sid", log: false });
            }
            cy.request({ url: "/api/method/frappe.auth.get_logged_user", log: false })
                .its("body.message", { log: false }).should("eq", user);
        });
    });
});

Cypress.Commands.add("call", (method, body) => {
    return cy.window().its("csrf_token").then((csrf) => {
        return cy.request({ method: "POST", url: "/api/method/" + method, body, headers: { "X-Frappe-CSRF-Token": csrf } });
    }).its("body.message");
});
