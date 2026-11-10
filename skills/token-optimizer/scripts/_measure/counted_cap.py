"""Exclude cap estimates contradicted by their own recorded evidence."""


def contradicted(alias):
    detail = alias + '.detail'
    chars = "CAST(REPLACE(substr({d}, instr({d}, '(')+1, instr({d}, ' chars >=')-instr({d}, '(')-1), ',', '') AS INTEGER)".format(d=detail)
    cap = "CAST(REPLACE(substr({d}, instr({d}, 'cap=')+4, instr({d}, ' tok,')-instr({d}, 'cap=')-4), ',', '') AS INTEGER)".format(d=detail)
    tool = "substr({d}, 12, instr(substr({d}, 12), ' result at cap')-1)".format(d=detail)
    return ("({a}.event_type = 'mcp_cap' AND ((instr({d}, ' chars >=') > 0 AND instr({d}, 'cap=') > 0 "
            "AND {chars} > 0 AND {cap} > 0 AND {chars} <= 4 * {cap}) OR "
            "(COALESCE({a}.session_id, '') != '' AND {tool} != '' AND EXISTS "
            "(SELECT 1 FROM main.savings_events ar WHERE ar.event_type = 'tool_archive' "
            "AND ar.session_id = {a}.session_id AND substr(ar.detail, 1, length('replaced ' || {tool} || ' (')) = 'replaced ' || {tool} || ' (' "
            "AND abs((julianday(ar.timestamp)-julianday({a}.timestamp))*86400) <= 3))))").format(
                a=alias, d=detail, chars=chars, cap=cap, tool=tool)


def reexpand_type(alias=''):
    """Separate Bash archive debits from MCP debits using recorded lineage."""
    prefix = alias + '.' if alias else ''
    detail = prefix + 'detail'
    kind = prefix + 'event_type'
    return ("CASE WHEN {kind}='tool_archive_reexpand' AND "
            "(instr(coalesce({detail},''), ' (Bash)') > 0 OR "
            "(length(substr(coalesce({detail},''),1,16))=16 AND "
            "substr({detail},1,16) NOT GLOB '*[^0-9a-f]*' AND "
            "(length({detail})=16 OR (substr({detail},17,1)=' ' AND instr({detail}, ' (')=0)))) "
            "THEN 'bash_archive_reexpand' ELSE {kind} END").format(kind=kind, detail=detail)
