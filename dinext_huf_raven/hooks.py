app_name = "dinext_huf_raven"
app_title = "Dinext Huf Raven"
app_publisher = "Dinext"
app_description = "Use Huf assistants in Raven with the employee's Frappe permissions."
app_email = "info@dinext.ru"
required_apps = ["raven", "huf"]

doc_events = {"Raven Message": {"after_insert": "dinext_huf_raven.bridge.on_message"}}
scheduler_events = {"cron": {"*/5 * * * *": ["dinext_huf_raven.bridge.recover"]}}
huf_tools = "dinext_huf_raven.registry.TOOLS"
