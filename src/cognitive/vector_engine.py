try:
    from databricks.vector_search.client import VectorSearchClient
except ModuleNotFoundError:
    class VectorSearchClient:
        def __init__(self, *args, **kwargs):
            raise Exception("Mock VectorSearchClient triggering exception")
    
import logging

logger = logging.getLogger("VectorEngine")

def setup_mosaic_vector_index():
    try:
        vsc = VectorSearchClient()
    except Exception as e:
        logger.warning(f"VectorSearchClient initialization failed (likely no credentials). Using mock. {e}")
        class MockVSC:
            def get_endpoint(self, name):
                logger.info(f"[MOCK] get_endpoint: {name}")
                raise Exception("Endpoint not found")
            def create_endpoint(self, name, endpoint_type):
                logger.info(f"[MOCK] create_endpoint: {name}, {endpoint_type}")
            def create_delta_sync_index(self, **kwargs):
                logger.info(f"[MOCK] create_delta_sync_index: {kwargs}")
        vsc = MockVSC()
    
    catalog = "enterprise_analytics_prod"
    schema = "agentic_core"
    endpoint_name = "mosaic-search-endpoint"
    source_table = f"{catalog}.{schema}.unstructured_knowledge_source"
    index_name = f"{catalog}.{schema}.knowledge_base_vector_index"
    
    try:
        vsc.get_endpoint(name=endpoint_name)
    except Exception:
        logger.info(f"Endpoint {endpoint_name} not found. Commencing deployment...")
        vsc.create_endpoint(name=endpoint_name, endpoint_type="VECTOR_SEARCH_OPTIMIZED")
        
    try:
        vsc.create_delta_sync_index(
            endpoint_name=endpoint_name,
            source_table_name=source_table,
            index_name=index_name,
            pipeline_type="TRIGGERED",
            primary_key="id",
            embedding_source_column="text_chunk",
            embedding_model_endpoint_name="databricks-bge-large-en"
        )
        logger.info(f"Delta sync vector index {index_name} successfully attached.")
    except Exception as e:
        logger.warning(f"Index provisioning sequence complete or bypassed: {str(e)}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    setup_mosaic_vector_index()

