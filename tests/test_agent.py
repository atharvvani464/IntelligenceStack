import pytest
from unittest.mock import MagicMock, patch
from src.cognitive.agent_core import MosaicAnalyticsAgent

@pytest.fixture
def mock_agent_context():
    with patch('src.cognitive.agent_core.SparkSession') as mock_spark_class, \
         patch('src.cognitive.agent_core.WorkspaceClient') as mock_sdk:
        mock_spark = mock_spark_class.builder.getOrCreate.return_value
        agent = MosaicAnalyticsAgent()
        yield agent, mock_spark, mock_sdk

def test_agent_routing_logic_with_customer_id(mock_agent_context):
    agent, mock_spark, mock_sdk = mock_agent_context
    
    mock_serving = MagicMock()
    mock_serving.choices = [MagicMock(message=MagicMock(content="Calling get_customer_anomaly_score for user validation."))]
    mock_sdk.return_value.serving_endpoints.query.return_value = mock_serving
    
    mock_df = MagicMock()
    mock_df.collect.return_value = [MagicMock(asDict=lambda: {"customer_id": "CUST_101", "total_anomalies": 4, "risk_factor": 12.45})]
    agent.spark.sql = MagicMock(return_value=mock_df)
    
    final_output = agent.run("Check anomalies for customer CUST_101")
    
    assert final_output is not None
    agent.spark.sql.assert_called_with("SELECT * FROM enterprise_analytics_prod.agentic_core.get_customer_anomaly_score('CUST_101')")
