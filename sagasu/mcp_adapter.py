"""Local MCP adapter policy. Tool definitions remain shared."""


class LocalAdapter:
    transport = "stdio"
    hidden_tools = frozenset()
    instructions = ""
    local_authentication_instruction = "Credentials stay on the host. When awaiting_mfa, read authentication_status and relay the prompt to the user. "
    login_description = None
    login_wait_seconds = None

    async def call_tool(self, checked, name, arguments, context, known_fields):
        return await checked(name, arguments, context)

    def update_status(self, status, service):
        pass

    def update_workflow(self, document):
        pass
