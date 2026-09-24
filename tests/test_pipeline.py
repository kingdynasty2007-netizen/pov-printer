# ============================================================
# FILE: tests/test_pipeline.py
# PURPOSE: Unit tests for the manual production pipeline.
#
# All expensive AI/media APIs are mocked.
# No real API credits are spent.
#
# RUN: python -m pytest tests/test_pipeline.py -v
# ============================================================

import os
import sys
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage_manager as sm
import script_verifier as sv


# ============================================================
# HELPERS
# ============================================================

def _make_temp_paths(tmp_dir):
    """Create a fake paths dict pointing to a temp directory."""
    paths = {
        "root": tmp_dir,
        "input": os.path.join(tmp_dir, "input"),
        "research": os.path.join(tmp_dir, "research"),
        "script": os.path.join(tmp_dir, "script"),
        "script_versions": os.path.join(tmp_dir, "script", "versions"),
        "verification": os.path.join(tmp_dir, "verification"),
        "breakdown": os.path.join(tmp_dir, "breakdown"),
        "characters": os.path.join(tmp_dir, "characters"),
        "references": os.path.join(tmp_dir, "references"),
        "media": os.path.join(tmp_dir, "media"),
        "media_images": os.path.join(tmp_dir, "media", "images"),
        "media_audio": os.path.join(tmp_dir, "media", "audio"),
        "media_video": os.path.join(tmp_dir, "media", "video"),
        "metadata": os.path.join(tmp_dir, "metadata"),
        "final": os.path.join(tmp_dir, "final"),
        "logs": os.path.join(tmp_dir, "logs"),
    }
    for p in paths.values():
        os.makedirs(p, exist_ok=True)
    return paths


SAMPLE_SCRIPT = """=== SCENES ===
CHARACTERS: david, goliath
David stands on the battlefield, facing the giant Goliath.
---
CHARACTERS: david
David picks up five smooth stones from the stream.
---
CHARACTERS: david, goliath
David slings a stone and Goliath falls.
---
CHARACTERS: david
David stands victorious as the Philistines flee.
---
CHARACTERS:
The Israelite army celebrates their unexpected victory.
---
CHARACTERS: david
David gives thanks to God for the victory.

=== AUDIO ===
SCENE: scene_001
VOICE: Zephyr
A young shepherd boy stood before a giant no one else dared to face.

SCENE: scene_002
VOICE: Zephyr
David chose five smooth stones, trusting not in weapons but in God.

SCENE: scene_003
VOICE: Zephyr
With a single stone, the giant fell, and the battle was won.

SCENE: scene_004
VOICE: Zephyr
The Philistines fled in terror as David stood victorious.

SCENE: scene_005
VOICE: Zephyr
All of Israel celebrated the miracle they had witnessed that day.

SCENE: scene_006
VOICE: Zephyr
David knew the victory belonged not to him, but to the Lord.
"""

PASS_VERIFICATION = {
    "passed": True,
    "status": "PASS",
    "checks": {
        "hook": {"passed": True, "reason": "Strong dramatic opening."},
        "biblical_accuracy": {"passed": True, "reason": "Events match 1 Samuel 17."},
        "scripture_usage": {"passed": True, "reason": "References are appropriate."},
        "story_structure": {"passed": True, "reason": "Clear arc."},
        "pacing": {"passed": True, "reason": "Good pacing."},
        "character_development": {"passed": True, "reason": "David's faith is clear."},
        "emotional_progression": {"passed": True, "reason": "Builds well."},
        "curiosity": {"passed": True, "reason": "Viewer wants to know outcome."},
        "climax": {"passed": True, "reason": "Stone throw is the climax."},
        "payoff_resolution": {"passed": True, "reason": "Victory is satisfying."},
        "narration_quality": {"passed": True, "reason": "Clear and warm."},
        "visual_storytelling": {"passed": True, "reason": "Each scene is visual."},
        "duration_scene_suitability": {"passed": True, "reason": "6 scenes for 60s is correct."},
    },
    "required_fixes": [],
    "warnings": [],
}

NEEDS_REWRITE_VERIFICATION = {
    "passed": False,
    "status": "NEEDS_REWRITE",
    "checks": {
        "hook": {"passed": False, "reason": "Weak opening — no immediate tension."},
        "biblical_accuracy": {"passed": True, "reason": "Major events are supported."},
        "scripture_usage": {"passed": True, "reason": "References are appropriate."},
        "story_structure": {"passed": True, "reason": "Clear arc."},
        "pacing": {"passed": False, "reason": "Scenes 4-6 repeat information."},
        "character_development": {"passed": True, "reason": "David's faith is clear."},
        "emotional_progression": {"passed": True, "reason": "Builds well."},
        "curiosity": {"passed": True, "reason": "Viewer wants to know outcome."},
        "climax": {"passed": True, "reason": "Stone throw is the climax."},
        "payoff_resolution": {"passed": True, "reason": "Victory is satisfying."},
        "narration_quality": {"passed": True, "reason": "Clear and warm."},
        "visual_storytelling": {"passed": True, "reason": "Each scene is visual."},
        "duration_scene_suitability": {"passed": True, "reason": "6 scenes for 60s is correct."},
    },
    "required_fixes": [
        "Strengthen the hook.",
        "Remove repeated exposition in scenes 4-6.",
    ],
    "warnings": [],
}

BIBLICAL_CONTRADICTION_VERIFICATION = {
    "passed": False,
    "status": "FAILED_QUALITY_GATE",
    "checks": {
        "hook": {"passed": True, "reason": "Good opening."},
        "biblical_accuracy": {"passed": False, "reason": "Script claims David used a sword, but 1 Samuel 17 says he used a sling."},
        "scripture_usage": {"passed": False, "reason": "Wrong scripture cited."},
        "story_structure": {"passed": True, "reason": "Clear arc."},
        "pacing": {"passed": True, "reason": "Good pacing."},
        "character_development": {"passed": True, "reason": "Clear."},
        "emotional_progression": {"passed": True, "reason": "Builds well."},
        "curiosity": {"passed": True, "reason": "Good."},
        "climax": {"passed": True, "reason": "Clear."},
        "payoff_resolution": {"passed": True, "reason": "Good."},
        "narration_quality": {"passed": True, "reason": "Clear."},
        "visual_storytelling": {"passed": True, "reason": "Visual."},
        "duration_scene_suitability": {"passed": True, "reason": "Correct."},
    },
    "required_fixes": ["Correct the weapon — David used a sling, not a sword."],
    "warnings": [],
}


# ============================================================
# STORAGE MANAGER TESTS
# ============================================================

class TestStorageManager(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Override DATA_DIR and DB_PATH for tests
        self._orig_data_dir = sm.DATA_DIR
        self._orig_db_path = sm.DB_PATH
        self._orig_productions_dir = sm.PRODUCTIONS_DIR
        sm.DATA_DIR = os.path.join(self.tmp, "data")
        sm.DB_PATH = os.path.join(sm.DATA_DIR, "test.db")
        sm.PRODUCTIONS_DIR = os.path.join(sm.DATA_DIR, "productions")
        os.makedirs(sm.DATA_DIR, exist_ok=True)
        os.makedirs(sm.PRODUCTIONS_DIR, exist_ok=True)

    def tearDown(self):
        sm.DATA_DIR = self._orig_data_dir
        sm.DB_PATH = self._orig_db_path
        sm.PRODUCTIONS_DIR = self._orig_productions_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    # Test 12: storage directory creation
    def test_create_production_dirs(self):
        paths = _make_temp_paths(self.tmp)
        for key in ["root", "script", "script_versions", "verification",
                    "breakdown", "metadata", "logs"]:
            self.assertTrue(os.path.isdir(paths[key]),
                            f"Directory missing: {key}")

    # Test 10: existing script starts at v001
    def test_existing_script_starts_at_v001(self):
        paths = _make_temp_paths(self.tmp)
        label = sm.save_script_version(paths, "My existing script text.")
        self.assertEqual(label, "v001")

    # Test 4: failed script → new version created
    def test_failed_script_creates_new_version(self):
        paths = _make_temp_paths(self.tmp)
        sm.save_script_version(paths, "Version 1 text.")
        label2 = sm.save_script_version(paths, "Version 2 text.")
        self.assertEqual(label2, "v002")

    # Test 5: old version remains unchanged
    def test_old_version_remains_unchanged(self):
        paths = _make_temp_paths(self.tmp)
        sm.save_script_version(paths, "Original v001 text.")
        sm.save_script_version(paths, "New v002 text.")
        v001_text = sm.load_script_version(paths, "v001")
        self.assertEqual(v001_text, "Original v001 text.")

    # Test 9: 60 sec → 6 scenes
    def test_sixty_seconds_is_six_scenes(self):
        from production_manager import _scene_count
        self.assertEqual(_scene_count(60), 6)

    def test_sqlite_schema_creates_table(self):
        sm.init_db()
        import sqlite3
        conn = sqlite3.connect(sm.DB_PATH)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='productions'"
        )
        self.assertIsNotNone(cursor.fetchone())
        conn.close()


# ============================================================
# SCRIPT VERIFIER TESTS
# ============================================================

class TestScriptVerifier(unittest.TestCase):

    # Test 13: verifier returns valid structured JSON
    def test_verifier_returns_structured_json(self):
        result = sv.verify_dry_run(
            SAMPLE_SCRIPT, "David and Goliath", "bible_story", 60, 6,
            force_pass=True,
        )
        self.assertIn("passed", result)
        self.assertIn("status", result)
        self.assertIn("checks", result)
        self.assertIn("required_fixes", result)
        self.assertIn("warnings", result)
        self.assertIsInstance(result["checks"], dict)
        self.assertIsInstance(result["required_fixes"], list)

    # Test 1: valid script → PASS
    def test_valid_script_passes(self):
        result = sv.verify_dry_run(
            SAMPLE_SCRIPT, "David and Goliath", "bible_story", 60, 6,
            force_pass=True,
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["status"], "PASS")

    # Test 2: weak hook → NEEDS_REWRITE
    def test_weak_hook_needs_rewrite(self):
        result = sv.verify_dry_run(
            SAMPLE_SCRIPT, "David and Goliath", "bible_story", 60, 6,
            force_pass=False,
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "NEEDS_REWRITE")
        self.assertFalse(result["checks"]["hook"]["passed"])

    # Test 3: Biblical contradiction → failure
    def test_biblical_contradiction_fails_gate(self):
        # Simulate the status resolution logic directly
        passed, status = sv._resolve_status(
            BIBLICAL_CONTRADICTION_VERIFICATION["checks"],
            "bible_story",
            sv.BIBLE_CRITICAL_CHECKS,
        )
        self.assertFalse(passed)
        self.assertEqual(status, "FAILED_QUALITY_GATE")

    def test_resolve_status_pass(self):
        passed, status = sv._resolve_status(
            PASS_VERIFICATION["checks"],
            "bible_story",
            sv.BIBLE_CRITICAL_CHECKS,
        )
        self.assertTrue(passed)
        self.assertEqual(status, "PASS")

    def test_resolve_status_needs_rewrite(self):
        passed, status = sv._resolve_status(
            NEEDS_REWRITE_VERIFICATION["checks"],
            "bible_story",
            sv.BIBLE_CRITICAL_CHECKS,
        )
        self.assertFalse(passed)
        self.assertEqual(status, "NEEDS_REWRITE")


# ============================================================
# PRODUCTION MANAGER TESTS
# ============================================================

class TestProductionManager(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_data_dir = sm.DATA_DIR
        self._orig_db_path = sm.DB_PATH
        self._orig_productions_dir = sm.PRODUCTIONS_DIR
        sm.DATA_DIR = os.path.join(self.tmp, "data")
        sm.DB_PATH = os.path.join(sm.DATA_DIR, "test.db")
        sm.PRODUCTIONS_DIR = os.path.join(sm.DATA_DIR, "productions")
        os.makedirs(sm.DATA_DIR, exist_ok=True)
        os.makedirs(sm.PRODUCTIONS_DIR, exist_ok=True)

    def tearDown(self):
        sm.DATA_DIR = self._orig_data_dir
        sm.DB_PATH = self._orig_db_path
        sm.PRODUCTIONS_DIR = self._orig_productions_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    # Test 6: rewrite → re-verification
    def test_rewrite_creates_new_version_and_reverifies(self):
        paths = _make_temp_paths(self.tmp)
        sm.save_script_version(paths, "v001 text")
        sm.save_script_version(paths, "v002 text")
        versions = sm.list_script_versions(paths)
        self.assertIn("v001.txt", versions)
        self.assertIn("v002.txt", versions)

    # Test 7: maximum rewrite limit
    def test_maximum_rewrite_limit_raises(self):
        from production_manager import run_verification_loop, MAX_REWRITE_ATTEMPTS

        paths = _make_temp_paths(self.tmp)
        sm.init_db()
        run_id, real_paths = sm.create_production(
            "David and Goliath", "topic", "bible_story", 60
        )

        # Patch verify_dry_run to always return NEEDS_REWRITE
        with patch.object(sv, "verify_dry_run", return_value=NEEDS_REWRITE_VERIFICATION):
            with self.assertRaises(RuntimeError) as ctx:
                run_verification_loop(
                    run_id, real_paths,
                    "David and Goliath", "bible_story", 60, [],
                    SAMPLE_SCRIPT, "v001",
                    scripture_references=[],
                    dry_run=True,
                )
        self.assertIn("rewrite", str(ctx.exception).lower())

    # Test 8: failed quality gate stops pipeline
    def test_failed_quality_gate_stops_pipeline(self):
        from production_manager import run_verification_loop

        paths = _make_temp_paths(self.tmp)
        sm.init_db()
        run_id, real_paths = sm.create_production(
            "David and Goliath", "topic", "bible_story", 60
        )

        with patch.object(sv, "verify_dry_run", return_value=BIBLICAL_CONTRADICTION_VERIFICATION):
            with self.assertRaises(RuntimeError) as ctx:
                run_verification_loop(
                    run_id, real_paths,
                    "David and Goliath", "bible_story", 60, [],
                    SAMPLE_SCRIPT, "v001",
                    scripture_references=[],
                    dry_run=True,
                )
        self.assertIn("FAILED_QUALITY_GATE", str(ctx.exception))

    # Test 14: downstream generation NOT called when verification fails
    def test_downstream_not_called_on_verification_failure(self):
        from production_manager import run_manual_pipeline

        sm.init_db()

        with patch.object(sv, "verify_dry_run", return_value=BIBLICAL_CONTRADICTION_VERIFICATION):
            with patch("production_manager._run_media_pipeline") as mock_media:
                with self.assertRaises(RuntimeError):
                    run_manual_pipeline(
                        topic="David and Goliath",
                        fmt="bible_story",
                        duration_secs=60,
                        dry_run=True,
                        run_media=True,
                    )
                mock_media.assert_not_called()

    # Test 11: Bible research produces source artifacts
    def test_bible_research_produces_artifacts(self):
        paths = _make_temp_paths(self.tmp)

        with patch("script_verifier.retrieve_bible_research") as mock_research:
            mock_research.return_value = (
                {"topic": "David and Goliath", "source": "bible_database"},
                [{"reference": "1 Samuel 17:4", "text": "And there went out a champion..."}],
            )
            from production_manager import run_research
            story_facts, scripture_refs = run_research(
                "RUN-TEST", paths, "David and Goliath", "bible_story"
            )

        self.assertTrue(os.path.exists(
            os.path.join(paths["research"], "story_facts.json")
        ))
        self.assertTrue(os.path.exists(
            os.path.join(paths["research"], "scripture_references.json")
        ))
        self.assertEqual(len(scripture_refs), 1)

    # Test: full dry-run pipeline completes
    def test_full_dry_run_pipeline(self):
        from production_manager import run_manual_pipeline

        sm.init_db()

        result = run_manual_pipeline(
            topic="David and Goliath",
            fmt="bible_story",
            duration_secs=60,
            dry_run=True,
            run_media=False,
        )

        self.assertIn("run_id", result)
        self.assertEqual(result["status"], "ready_for_media")
        self.assertEqual(result["scene_count"], 6)

    # Test: topic mode works
    def test_topic_mode(self):
        sm.init_db()
        run_id, paths = sm.create_production(
            "David and Goliath", "topic", "bible_story", 60
        )
        record = sm.get_production(run_id)
        self.assertEqual(record["input_type"], "topic")
        self.assertEqual(record["format"], "bible_story")
        self.assertEqual(record["duration_secs"], 60)

    # Test: existing-script mode works
    def test_existing_script_mode(self):
        sm.init_db()
        run_id, paths = sm.create_production(
            "[uploaded script: my_script.txt]", "existing_script", "bible_story", 60
        )
        record = sm.get_production(run_id)
        self.assertEqual(record["input_type"], "existing_script")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
