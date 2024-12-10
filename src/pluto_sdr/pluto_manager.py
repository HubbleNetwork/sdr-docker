from pluto_sdr import PlutoRX, PlutoTX


class PlutoManager:
    def __init__(self):
        self.pluto = None

    def initialize(self, mode):

        if mode == "tx":
            if self.is_tx_mode():
                return
            if self.is_initialized():
                del self.pluto
            self.pluto = PlutoTX()
        elif mode == "rx":
            if self.is_rx_mode():
                return
            if self.is_initialized():
                del self.pluto
            self.pluto = PlutoRX()
        else:
            raise ValueError(f"Invalid mode: {mode}")

    def is_initialized(self):
        return self.pluto is not None

    def is_tx_mode(self):
        return isinstance(self.pluto, PlutoTX)

    def is_rx_mode(self):
        return isinstance(self.pluto, PlutoRX)
