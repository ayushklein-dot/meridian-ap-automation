import unittest
import langgraph_runtime

class TestLangGraphRuntime(unittest.TestCase):
    def test_graph_builder_is_callable_or_has_clear_dependency_error(self):
        if langgraph_runtime.StateGraph is None:
            with self.assertRaisesRegex(RuntimeError, "LangGraph is not installed"):
                langgraph_runtime.build_langgraph()
        else:
            app = langgraph_runtime.build_langgraph()
            self.assertTrue(hasattr(app, "invoke"))

    def test_state_type_exists(self):
        self.assertTrue(hasattr(langgraph_runtime, "InvoiceState"))

    def test_internal_context_fields_are_in_graph_schema(self):
        annotations = langgraph_runtime.InvoiceState.__annotations__
        for field in ("_existing_cases", "_approver_role", "_approve", "_cash_limit", "_simulated_reply"):
            self.assertIn(field, annotations)

    def test_conditional_routes_are_fail_closed(self):
        self.assertEqual(langgraph_runtime._after_intake({"next_node": "END"}), "end")
        self.assertEqual(langgraph_runtime._after_validation({"next_node": "END"}), "end")
        self.assertEqual(langgraph_runtime._after_exception({"next_node": "END"}), "end")
        self.assertEqual(langgraph_runtime._after_approval({"next_node": "END"}), "end")

    def test_conditional_routes_cover_expected_nodes(self):
        self.assertEqual(langgraph_runtime._after_intake({"next_node": "validation"}), "validation")
        self.assertEqual(langgraph_runtime._after_validation({"next_node": "exception"}), "exception_resolution")
        self.assertEqual(langgraph_runtime._after_validation({"next_node": "approval"}), "approval")
        self.assertEqual(langgraph_runtime._after_exception({"next_node": "exception"}), "exception_resolution")
        self.assertEqual(langgraph_runtime._after_approval({"next_node": "payment"}), "payment_reconciliation")

if __name__ == "__main__":
    unittest.main()
