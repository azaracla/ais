import boto3
import os
from configuration import *

def test_s3():
    print(f"🔍 Testing S3 connection to {OVH_ENDPOINT}...")
    print(f"🔑 Access Key: {OVH_ACCESS_KEY[:5]}...{OVH_ACCESS_KEY[-5:]}")
    
    from botocore.client import Config
    s3 = boto3.client(
        's3',
        endpoint_url=OVH_ENDPOINT,
        aws_access_key_id=OVH_ACCESS_KEY,
        aws_secret_access_key=OVH_SECRET_KEY,
        config=Config(s3={'addressing_style': 'path'})
    )

    # 1. Skip ListBuckets (often forbidden for sub-users)
    print("📁 (Skipping ListBuckets - often restricted)")
    
    try:
        # 2. Try to list objects in the target bucket
        print(f"📂 Listing objects in {BUCKET_RAW}...")
        s3.list_objects_v2(Bucket=BUCKET_RAW, MaxKeys=5)
        print(f"✅ Listing {BUCKET_RAW} successful!")
        
        # 3. Try to upload a small file
        test_file = "test_connection.txt"
        with open(test_file, "w") as f:
            f.write("Hello AIS DuckLake!")
            
        s3_key = f"test/{test_file}"
        print(f"📤 Uploading test file to {BUCKET_RAW}/{s3_key}...")
        s3.upload_file(test_file, BUCKET_RAW, s3_key)
        print("✅ Upload successful!")
        
        # 4. Clean up
        print(f"🗑️ Deleting test file from S3...")
        s3.delete_object(Bucket=BUCKET_RAW, Key=s3_key)
        print("✅ Deletion successful!")
        os.remove(test_file)
        print("🎉 All tests passed!")
        
    except Exception as e:
        print(f"❌ S3 Test Failed: {e}")

if __name__ == "__main__":
    test_s3()
