"""Test script to read and display .pckl files from .langgraph_api folder."""

import os
import pickle
from pathlib import Path


def read_pckl_file(file_path: str) -> None:
    """Read and display contents of a .pckl file in human-readable format.
    
    Args:
        file_path: Path to the .pckl file
    """
    print(f"\n{'=' * 80}")
    print(f"File: {os.path.basename(file_path)}")
    print(f"{'=' * 80}")

    try:
        with open(file_path, 'rb') as f:
            data = pickle.load(f)

        print(f"Type: {type(data).__name__}")
        print(f"\nContents:")
        print("-" * 80)

        # Handle different data types
        if isinstance(data, dict):
            print(f"Dictionary with {len(data)} keys:")
            for key, value in data.items():
                print(f"\n  Key: {key}")
                print(f"  Value Type: {type(value).__name__}")
                if isinstance(value, (dict, list, tuple)):
                    print(f"  Length/Size: {len(value)}")
                # Print first few items for collections
                if isinstance(value, dict) and len(value) > 0:
                    print(f"  First few keys: {list(value.keys())[:5]}")
                elif isinstance(value, (list, tuple)) and len(value) > 0:
                    print(f"  First few items: {value[:3]}")
                else:
                    # Truncate long string representations
                    value_str = str(value)
                    if len(value_str) > 200:
                        print(f"  Value: {value_str[:200]}...")
                    else:
                        print(f"  Value: {value}")

        elif isinstance(data, (list, tuple)):
            print(f"{type(data).__name__} with {len(data)} items:")
            for i, item in enumerate(data[:10]):  # Show first 10 items
                print(f"\n  [{i}] Type: {type(item).__name__}")
                item_str = str(item)
                if len(item_str) > 200:
                    print(f"      Value: {item_str[:200]}...")
                else:
                    print(f"      Value: {item}")
            if len(data) > 10:
                print(f"\n  ... and {len(data) - 10} more items")

        else:
            # For other types, print the representation
            data_str = str(data)
            if len(data_str) > 500:
                print(data_str[:500] + "...")
            else:
                print(data_str)

    except Exception as e:
        print(f"Error reading file: {e}")
        import traceback
        traceback.print_exc()


def main():
    """Main function to loop through all .pckl files and display their contents."""
    # Get the path to .langgraph_api folder
    script_dir = Path(__file__).parent.parent
    langgraph_api_dir = script_dir / ".langgraph_api"

    if not langgraph_api_dir.exists():
        print(f"Error: Directory {langgraph_api_dir} does not exist!")
        return

    # Find all .pckl files
    pckl_files = sorted(langgraph_api_dir.glob("*.pckl"))

    if not pckl_files:
        print(f"No .pckl files found in {langgraph_api_dir}")
        return

    print(f"Found {len(pckl_files)} .pckl file(s) in {langgraph_api_dir}")
    print(f"{'=' * 80}")

    # Process each file
    for i, pckl_file in enumerate(pckl_files, 1):
        read_pckl_file(str(pckl_file))

        # Wait for user input before continuing (except after the last file)
        if i < len(pckl_files):
            print(f"\n[{i}/{len(pckl_files)} files processed]")
            input("Press Enter to continue to the next file...")

    print(f"\n{'=' * 80}")
    print(f"Completed processing {len(pckl_files)} file(s)")
    print(f"{'=' * 80}\n")


if __name__ == "__main__":
    main()
