# Published model provenance

`parente_5_2.npz` contains converted Dense weights from `CryptoTrading/model_final_5_2.h5`, reconstructed training-scaler statistics and frozen historical base-volume calibration.

Authors: Mimmo Parente, Luca Rizzuti and Mario Trerotola (paper); the Figshare software entry credits Mimmo Parente and Luca Rizzuti.

Source and corresponding training code: https://figshare.com/articles/code/CryptoTrading_zip/22953377/2

Paper: https://doi.org/10.1016/j.eswa.2023.121806

The software archive is offered under GPL 3.0+ (GPL-3.0-or-later): https://www.gnu.org/licenses/gpl-3.0.html . Its data notice asks users to check Binance terms for financial-data usage. The attached `parente_5_2.json` records the source, hashes, schema and calibration provenance. Conversion is implemented in `adaptive_crypto/neural_tools.py`; the original research scripts are not executed by the application.

The conversion changes the storage format and freezes normalization for causal inference. It does not retrain the weights. `test_data/neural_author_btc.json` contains a small archived BTC reference extract used to verify feature calculations, plus independently calculated network outputs. No profitability claim accompanies these artifacts.
