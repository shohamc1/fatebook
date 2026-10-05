# fatebook — build the FSNr EPK decryptor, then run the pipeline.
#
# Targets:
#   make tool    clone + compile FSNr_tools (g++ / Apple Clang / mingw)
#   make run     extract the game data and build the EPUBs (uv run fsn-book)
#   make         same as make run
#   make clean   remove all build output (extracted data, work dirs, books)

FSNR_DIR := tools_fsnr
FSNR_EXE := $(FSNR_DIR)/build/main$(if $(filter Windows_NT,$(OS)),.exe)

.PHONY: all run tool validate clean

all: run

run: tool
	uv run fsn-book
	uv run fsn-validate

tool: $(FSNR_EXE)

$(FSNR_EXE):
	git clone https://github.com/kurikomoe/FSNr_tools $(FSNR_DIR)
	mkdir -p $(FSNR_DIR)/build
	cd $(FSNR_DIR) && \
		g++ --std=c++20 -O2 main.cpp -o build/main$(if $(filter Windows_NT,$(OS)),.exe)
	@# FSNr's main resolves SomeKey.bin relative to itself
	@if [ ! -f $(FSNR_DIR)/build/SomeKey.bin ] && [ -f $(FSNR_DIR)/SomeKey.bin ]; then \
		cp $(FSNR_DIR)/SomeKey.bin $(FSNR_DIR)/build/; fi

validate:
	uv run fsn-validate

clean:
	rm -rf output
