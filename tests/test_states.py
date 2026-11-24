import unittest
from collections import deque

from agentgov.errors import ErrorCode, GovernanceError
from agentgov.states import EVENTS, TERMINAL, TRANSITIONS, State, allowed_events, transition


class StateMachineTests(unittest.TestCase):
    def test_every_listed_edge_is_accepted(self) -> None:
        for (source, event), targets in TRANSITIONS.items():
            for target in targets:
                self.assertEqual(transition(source, event, target), target)

    def test_every_unlisted_edge_is_rejected(self) -> None:
        rejected = 0
        for source in State:
            for event in EVENTS:
                for target in State:
                    if target in TRANSITIONS.get((source, event), frozenset()):
                        continue
                    with self.assertRaises(GovernanceError) as caught:
                        transition(source, event, target)
                    self.assertEqual(caught.exception.code, ErrorCode.INVALID_TRANSITION)
                    rejected += 1
        self.assertGreater(rejected, 1000)

    def test_unknown_event_is_rejected(self) -> None:
        with self.assertRaises(GovernanceError):
            transition(State.CREATED, "teleport", State.COMPLETED)

    def test_terminal_states_have_no_exits(self) -> None:
        self.assertEqual(TERMINAL, {State.COMPLETED, State.BLOCKED})
        for state in TERMINAL:
            self.assertEqual(allowed_events(state), [])

    def test_no_shortcut_from_created_to_execution(self) -> None:
        for event in EVENTS:
            for target in (State.AUTHORIZED, State.EXECUTING, State.VERIFIED, State.COMPLETED):
                self.assertNotIn(target, TRANSITIONS.get((State.CREATED, event), frozenset()))

    def test_execution_is_only_entered_from_authorized(self) -> None:
        sources = {source for (source, _), targets in TRANSITIONS.items() if State.EXECUTING in targets}
        self.assertEqual(sources, {State.AUTHORIZED})

    def test_completed_is_only_reached_after_verification(self) -> None:
        sources = {source for (source, _), targets in TRANSITIONS.items() if State.COMPLETED in targets}
        self.assertEqual(sources, {State.VERIFIED})

    def test_every_state_is_reachable_and_every_non_terminal_can_reach_a_terminal(self) -> None:
        graph: dict[State, set[State]] = {state: set() for state in State}
        for (source, _), targets in TRANSITIONS.items():
            graph[source] |= targets
        seen, queue = {State.CREATED}, deque([State.CREATED])
        while queue:
            for nxt in graph[queue.popleft()]:
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        self.assertEqual(seen, set(State))
        for state in set(State) - TERMINAL:
            self.assertIn(State.BLOCKED, graph[state], f"{state} cannot be blocked")

    def test_amendment_is_impossible_once_execution_started(self) -> None:
        for state in (State.EXECUTING, State.VERIFIED, State.COMPLETED, State.BLOCKED):
            self.assertNotIn("amend", allowed_events(state))


if __name__ == "__main__":
    unittest.main()
