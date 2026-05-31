#!/usr/bin/env python3
"""
Icon converter for Random Image Viewer
Converts the SVG icon to ICO format for Windows executables
"""

try:
    from PIL import Image
    import cairosvg
    import io
    print("Converting icon.svg to icon.ico...")
    
    # Convert SVG to PNG in memory
    png_data = cairosvg.svg2png(url="icon.svg", output_width=256, output_height=256)
    
    # Open PNG data with PIL
    image = Image.open(io.BytesIO(png_data))
    
    # Create ICO file with multiple sizes
    image.save("icon.ico", format="ICO", sizes=[(16,16), (32,32), (48,48), (64,64), (128,128), (256,256)])
    print("✅ Successfully created icon.ico")
    
except ImportError as e:
    print("❌ Missing dependencies. Install with:")
    print("pip install pillow cairosvg")
    print(f"Error: {e}")
    
except Exception as e:
    print(f"❌ Error converting icon: {e}")
    print("You can use online converters like:")
    print("- https://convertio.co/svg-ico/")
    print("- https://cloudconvert.com/svg-to-ico")
