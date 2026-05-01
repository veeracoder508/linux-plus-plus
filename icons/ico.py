from PIL import Image # type: ignore

# 1. List of the hex colors you want to make transparent
hex_colors = [
    '#E3E3E3', '#DEDEDE', '#DDDDDD', '#D7D7D7', "#d8d8d8", "#dbdbdb", "#dcdcdc", "#dadada", "#d9d9d9",
    '#FCFCFC', '#FFFFFF', '#F9F9F9', '#EDEDED', '#EBEBEB', '#efefef', '#fefefe',
    '#ececec', "#eeeeee", "#e1e1e1", "#e6e6e6", "#e2e2e2", "#e5e5e5", "#e0e0e0", "#e8e8e8", "#e9e9e9", "#eaeaea", "#e7e7e7", "#e4e4e4",
    "#dfdfdf", "#fdfdfd", "#e1e1de", "#dededd", "#e1e1df", "#fbfbfb", "#fafafa", "#fcfcfc", "#f7f7f7", "#f1f1f1", "#f0f0f0", "#f2f2f2","#f8f8f8",
    "#e1e2e1", "#fbfcfb", "#F9F8F8", "#DEE1DF", "#FFFEFF", "#FCFBFC", "#E3E4E3", "#E1E3E2", "#DEE0DF", "#FEFDFE", "#FDFCFC", "#FDFEFD", 
    "#F6F6F6", 
    "#F5F5F5", "#F4F4F4", "#F3F3F3", "#EEEEEE", "#EAEAEA", "#E9E9E9", "#E8E8E8", "#E7E7E7",
    "#DCDCDC", "#D3D3D3", "#C0C0C0"
]

# 2. Helper function to convert Hex codes to RGB tuples
def hex_to_rgb(hex_code):
    hex_code = hex_code.lstrip('#')
    return tuple(int(hex_code[i:i+2], 16) for i in (0, 2, 4))

# Create a list of RGB tuples to check against
target_colors = [hex_to_rgb(h) for h in hex_colors]

# 3. Open the image and ensure it has an Alpha channel
# Replace with your actual file name
img = Image.open("linux_plus_plusx250.ico").convert("RGBA")

# 4. Get all the pixel data
pixels = img.getdata()
new_pixels = []

# 5. Loop through every pixel
for pixel in pixels:
    # pixel is a tuple: (Red, Green, Blue, Alpha)
    # We only check the first 3 values (RGB) against our target list

    # Improved check: look for exact matches in our list OR any color 
    # that is generally "white-ish" based on a threshold (R, G, B > 205).
    if pixel[:3] in target_colors or all(c > 205 for c in pixel[:3]):
        # If it matches, replace it with a fully transparent pixel
        new_pixels.append((0, 0, 0, 0))
    else:
        # Otherwise, make it a solid black pixel
        new_pixels.append((0, 0, 0, 255))

# 6. Apply the new pixels to the image
img.putdata(new_pixels)

# 7. Save the result
# Saving as PNG is usually best for testing transparent outputs
img.save("cleaned_transparent_icon.png")

# If you want to save it directly back to an ICO format:
# img.save("cleaned_icon.ico", format="ICO", sizes=[(16,16), (32,32), (64,64), (128,128), (256,256)])

print("Specified background colors successfully removed!")