"""CPU export contract checks for the optional offline training environment."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

import numpy as np

from tools.candidate_train_pipeline import ARCHITECTURES, export_onnx, model_class, set_seed


@unittest.skipUnless(all(importlib.util.find_spec(name) is not None
                         for name in ("torch", "onnx", "onnxruntime")),
                     "Optional isolated PyTorch/ONNX training environment")
class CandidateExportTests(unittest.TestCase):
    def test_each_distinct_architecture_exports_static_cpu_logits_with_parity(self):
        set_seed(430)
        values = np.random.default_rng(430).normal(size=(4, 64, 16)).astype(np.float32)
        shapes = set()
        with tempfile.TemporaryDirectory() as temporary:
            for architecture in ARCHITECTURES:
                model = model_class(architecture)()
                shapes.add((type(model).__name__, sum(p.numel() for p in model.parameters())))
                result = export_onnx(model, Path(temporary) / f"{architecture}.onnx", values)
                self.assertLessEqual(result["max_abs_logits_difference"], result["tolerance"])
                self.assertGreater(result["onnx_bytes"], 1000)
            self.assertEqual(len(shapes), 4)


if __name__ == "__main__":
    unittest.main()
