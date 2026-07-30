# -*- coding: utf-8 -*-
"""pytest 부트스트랩 - 레포 루트를 import 경로에 올린다.

기존 테스트는 각자 sys.path.insert 로 자립 실행(python tests/x.py)을 지원한다.
그 방식은 유지하되, 이 파일이 있으면 `pytest` 한 줄로 전 테스트를 돌릴 수 있다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
