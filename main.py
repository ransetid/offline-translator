"""py2app / 直接运行入口：python3 main.py（等价于 python3 -m livetranslator）。"""
import sys

from livetranslator.app import main

if __name__ == "__main__":
    main(sys.argv[1:])
