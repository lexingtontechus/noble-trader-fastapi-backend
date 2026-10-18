import shutil, os

# Copy USDCHF from profile dir to repo
src_usdchf = r"C:\Users\aloys\AppData\Local\hermes\profiles\astro\docs\signals-health-eod-digests\2026-09-03-USDCHF.json"
dst_usdchf = r"C:\Users\aloys\OneDrive\Documents\GitHub\noble-trader-workspace\nobletradingapp\docs\signals-health-eod-digests\2026-09-03-USDCHF.json"
shutil.copy2(src_usdchf, dst_usdchf)
print(f"USDCHF copied: {os.path.exists(dst_usdchf)}")

# Copy US500 from nested path
src_us500 = r"C:\Users\aloys\OneDrive\Documents\GitHub\noble-trader-workspace\nobletradingapp\docs\signals-health-eod-digests\docs\signals-health-eod-digests\2026-09-03-US500.json"
dst_us500 = r"C:\Users\aloys\OneDrive\Documents\GitHub\noble-trader-workspace\nobletradingapp\docs\signals-health-eod-digests\2026-09-03-US500.json"
shutil.copy2(src_us500, dst_us500)
print(f"US500 copied: {os.path.exists(dst_us500)}")

# Remove nested directory
nested = r"C:\Users\aloys\OneDrive\Documents\GitHub\noble-trader-workspace\nobletradingapp\docs\signals-health-eod-digests\docs"
shutil.rmtree(nested, ignore_errors=True)
print(f"Nested dir removed: {not os.path.exists(nested)}")

# List final directory
dst_dir = r"C:\Users\aloys\OneDrive\Documents\GitHub\noble-trader-workspace\nobletradingapp\docs\signals-health-eod-digests"
print("\n--- Final directory listing ---")
for f in sorted(os.listdir(dst_dir)):
    fpath = os.path.join(dst_dir, f)
    size = os.path.getsize(fpath)
    print(f"  {f} ({size} bytes)")
