import argparse
import asyncio
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from agent import root_agent
import os, sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
app_name = "coding_agent"
async def async_runner_init(user_id, session_id):
    session_service = InMemorySessionService()
    init_state=None
    session = await session_service.create_session(
        app_name=app_name, user_id=user_id, session_id=session_id,
        state=init_state
    )
    if not session:
        session = await session_service.create_session(
            app_name=app_name, user_id=user_id,
            state=init_state
        )

    agent_instance = root_agent
    runner = Runner(agent=agent_instance, app_name=app_name, session_service=session_service)

    return runner, session

async def async_runner_call(query, runner, user_id, session_id):
    content = types.Content(role="user", parts=[types.Part(text=query)])
    done=False
    async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=content):
        if done is True:
            # We don't really need a break here because the async for even loop should quit automatically 
            # when the agent has no more task to do (after last response) and returns.
            #break
            pass

        calls = event.get_function_calls()
        if calls:
            for call in calls:
                tool_name = call.name
                arguments = call.args
                print(f"Agent calling tool: {tool_name}")
                print(f"    Arguments: {arguments}")

        if event.is_final_response():
            if event.content and event.content.parts:
                final_response_text = event.content.parts[0].text
            elif event.actions and event.actions.escalate: # Handle potential errors/escalations
                final_response_text = f"Agent escalated: {event.error_message or 'No specific message.'}"
            done=True
            # should not break the loop here, because runner.run_async need run once to close the invocation
            # after it yields the final response.
            # break 
        
    print(f"\n\u2728 Agent Response: {final_response_text}\n")

async def async_main(user_id, session_id, query):
    # We must get the actual session object, because,
    # 1. the created session id value may not be the same as the input session_id.
    # 2. the session_id input may be None, in which case we need to use the actual session id.
    runner, session = await async_runner_init(user_id, session_id)

    if query:
        print(f"\n[User]: {query}")
    else:
        query =input("\n[User] ('quit' to exit): ")

    while True:
        if query.lower() == 'quit' or query.lower() == 'exit':
            break
        await async_runner_call(query, runner, user_id, session.id)
        query =input("\n[User] ('quit' to exit): ")

    # Get final output from session. We use get_session to get a copy of session object. Because,
    # 1. The output_key of the agent session state is only updated after the agent is closed.
    # 2. We cannot inspect the final state of the session when the runner is running. 
    # 3. We make a copy of the session object to inspect the session final state after the runner is closed.
    session = await runner.session_service.get_session(app_name=app_name, user_id=user_id, session_id=session.id)
    output_key = runner.agent.output_key
    await runner.close()
    if session.state.get(output_key):
        print(f"Final output: {session.state[output_key]}")
    else:
        print("No final output.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", default="user_1")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--query", default=None)
    args = parser.parse_args()
    try:
        asyncio.run(async_main(args.user_id, args.session_id, args.query))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
