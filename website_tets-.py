import requests

def test_website_loading():
    url = 'https://atg.world'
    response = requests.get(url)
    if response.status_code == 200:
        print("Website loaded successfully!")
    else:
        print("Failed to load website!")

if __name__ == '__main__':
    test_website_loading()