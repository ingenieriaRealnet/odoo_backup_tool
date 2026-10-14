"""
External API connections (Odoo JSON-2, generic REST...) managed by the tool.

    connections.py   ApiConnectionManager: api_connections.json (no secrets)
    credentials.py   secrets in the Windows Credential Manager
    connectors.py    one connector class per kind of system + CONNECTOR_TYPES registry
    realnet_board.py Realnet task board / helpdesk service on an odoo_json2 connection

Entry points: the "Conexiones API" page (gui/api_panel.py) and api_cli.py.
"""
