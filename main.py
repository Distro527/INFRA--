#!/usr/bin/env python3
"""
VoidSyn - Main entry point for the executable version
"""
import sys
import os
import threading
import webbrowser
import time
from app import create_app

def open_browser():
    """Open the default web browser after a short delay"""
    time.sleep(1.5)  # Give the server time to start
    webbrowser.open('http://127.0.0.1:5000')

def main():
    """Main function to run the Flask application"""
    try:
        # Create the Flask app
        app = create_app()
        
        # Start browser in a separate thread
        browser_thread = threading.Thread(target=open_browser)
        browser_thread.daemon = True
        browser_thread.start()
        
        print("Starting VoidSyn...")
        print("Server will be available at: http://127.0.0.1:5000")
        print("Press Ctrl+C to stop the server")
        
        # Run the Flask app
        app.run(
            host='127.0.0.1',
            port=5000,
            debug=False,  # Disable debug mode for production
            use_reloader=False  # Disable auto-reloader for executable
        )
        
    except KeyboardInterrupt:
        print("\nShutting down VoidSyn...")
        sys.exit(0)
    except Exception as e:
        print(f"Error starting VoidSyn: {e}")
        input("Press Enter to exit...")
        sys.exit(1)

if __name__ == '__main__':
    main()
