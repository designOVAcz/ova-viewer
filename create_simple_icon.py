#!/usr/bin/env python3
"""
Create a simple PNG icon from scratch for the Random Image Viewer
This creates a basic icon without external dependencies
"""

try:
    from PIL import Image, ImageDraw
    
    # Create a 64x64 image
    size = 64
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # Blue circle background
    draw.ellipse([4, 4, size-4, size-4], fill=(45, 90, 160), outline=(30, 63, 115), width=2)
    
    # White photo frame
    frame_margin = 12
    draw.rectangle([frame_margin, frame_margin+3, size-frame_margin, size-frame_margin-3], 
                   fill=(255, 255, 255), outline=(200, 200, 200), width=1)
    
    # Simple mountain landscape
    draw.polygon([(frame_margin+2, size-frame_margin-5), 
                  (frame_margin+12, frame_margin+8), 
                  (frame_margin+22, frame_margin+15), 
                  (frame_margin+32, frame_margin+5), 
                  (size-frame_margin-8, frame_margin+12),
                  (size-frame_margin-2, size-frame_margin-5)], 
                 fill=(74, 144, 226))
    
    # Sun
    draw.ellipse([size-frame_margin-12, frame_margin+5, size-frame_margin-4, frame_margin+13], 
                 fill=(255, 213, 79))
    
    # Save as PNG and ICO
    img.save('icon.png', 'PNG')
    img.save('icon.ico', 'ICO', sizes=[(16,16), (32,32), (48,48), (64,64)])
    print("✅ Created icon.png and icon.ico successfully!")
    
except ImportError:
    print("❌ PIL (Pillow) not installed. Install with: pip install pillow")
except Exception as e:
    print(f"❌ Error creating icon: {e}")
