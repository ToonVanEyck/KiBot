#!/usr/bin/python3
import os
import sys
# Setup the path to load local kibot module
prev_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if prev_dir not in sys.path:
    sys.path.insert(0, prev_dir)
# Force the pcbnew module load to fail
sys.modules['colorama'] = None
# Import the module to test
from kibot import log
log.set_domain('kibot')
logger = log.init()
logger.error('Testing 1 2 3')

