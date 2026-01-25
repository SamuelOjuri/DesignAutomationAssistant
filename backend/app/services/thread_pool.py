from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple, Callable, Any

def process_items_in_parallel(items: List[Tuple[str, Any]], process_func: Callable, max_workers: int = 15):
    results = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as executor:
        future_to_item = {
            executor.submit(process_func, item_type, item): (item_type, item)
            for item_type, item in items
        }
        for future in as_completed(future_to_item):
            item_type, item = future_to_item[future]
            try:
                filename, text = future.result()
            except Exception as e:
                if isinstance(item, dict):
                    filename = item.get("filename", "unknown")
                elif isinstance(item, list) and item and isinstance(item[0], dict):
                    filename = f"batch_of_{len(item)}_items"
                else:
                    filename = item[0] if item else "unknown"
                text = f"Error processing {filename}: {e}"
            results.append((filename, text))
    return results