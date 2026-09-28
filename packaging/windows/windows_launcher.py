"""Entry point frozen into pyPTA.exe."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from adaptive_crypto.desktop import main
    raise SystemExit(main())
