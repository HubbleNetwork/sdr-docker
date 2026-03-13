import multiprocessing

from stream_web.app import main

if __name__ == "__main__":
    multiprocessing.set_start_method("fork", force=True)
    main()
