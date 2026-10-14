package main

// allLibraries is every library the harness measures, in the order of the report.
func allLibraries() []Library {
	libs := []Library{
		libYAMLv3(), libYAMLv4(), libYAMLv2(), libSigs(), libGoccy(),
		libPalantir(), libPalantirGoccy(), libJSONPatch(),
	}
	libs = append(libs, ownLibraries()...)
	return append(libs, controlLibraries()...)
}
