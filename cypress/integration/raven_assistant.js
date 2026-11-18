const scenario = "__TEST_SCENARIO__:TEST_TEXT";
const modelReply = "This is a deterministic TEST_TEXT response from the HUF test provider.";
const notice = "I replied in your direct chat.";
const part = encodeURIComponent;

describe("Dinext assistant in Raven", () => {
    let fixture;
    const dm = (id) => "/raven/dm-channel/" + part(id);
    const channel = () => "/raven/" + part(fixture.workspace) + "/" + part(fixture.channel);
    const showUser = (index, url) => {
        cy.login(index);
        cy.visit(url);
    };
    const mention = (text) => {
        return cy.call("raven.api.raven_message.send_message", {
            channel_id: fixture.channel,
            text: '<p><span data-type="userMention" data-id="' + fixture.bot + '">@QA</span> ' + text + "</p>",
            send_silently: true,
        }).then((doc) => cy.task("assistantReceipt", { source: doc.name }));
    };

    before(() => cy.task("assistantFixture", null, { log: false }).then((value) => { fixture = value; }));

    it("receives a Huf response after an employee sends a DM from the Raven composer", () => {
        showUser(0, dm(fixture.dms[0]));
        const text = scenario + " personal " + fixture.tag;
        // Insert the literal test marker as one input, so Tiptap's keystroke-level
        // Markdown shortcuts do not treat its double underscores as bold formatting.
        cy.get('.ProseMirror[contenteditable="true"]').filter(":visible").last().click().then(($editor) => {
            $editor[0].ownerDocument.execCommand("insertText", false, text);
        }).type("{enter}");
        cy.contains(text).should("be.visible");
        cy.task("assistantReceipt", { text }).then((event) => {
            cy.reload();
            cy.get('[data-message-id="' + event.reply + '"]').contains(modelReply).should("be.visible");
        });
    });

    it("keeps a shared-channel answer in the employee's DM and leaves a neutral notice in the thread", () => {
        showUser(1, channel());
        mention(scenario + " shared " + fixture.tag).then((event) => {
            cy.visit(channel() + "/thread/" + part(event.source));
            cy.get('[data-message-id="' + event.notice + '"]').contains(notice).should("be.visible");
            // Inspect the thread's actual message elements. The DM sidebar may show
            // this user's own private conversation previews on the very same page.
            cy.get('[data-message-id="' + event.reply + '"]').should("not.exist");
            showUser(1, dm(fixture.dms[1]));
            cy.get('[data-message-id="' + event.reply + '"]').contains(modelReply).should("be.visible");
            showUser(0, channel() + "/thread/" + part(event.source));
            cy.get('[data-message-id="' + event.notice + '"]').contains(notice).should("be.visible");
            cy.get('[data-message-id="' + event.reply + '"]').should("not.exist");
        });
    });

    it("continues the source thread in the requesting employee's Huf conversation", () => {
        showUser(0, channel());
        mention(scenario + " start thread " + fixture.tag).then((initial) => {
            cy.visit(channel() + "/thread/" + part(initial.source));
            cy.contains(notice).should("be.visible");
            cy.call("raven.api.raven_message.send_message", {
                channel_id: initial.source, text: "<p>" + scenario + " follow-up</p>", send_silently: true,
            }).then((doc) => cy.task("assistantReceipt", { source: doc.name })).then((reply) => {
                expect(reply.conversation).to.equal(initial.conversation);
                expect(reply.run).not.to.equal(initial.run);
            });
        });
    });
});
