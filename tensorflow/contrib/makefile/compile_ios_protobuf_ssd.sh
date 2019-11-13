#!/bin/bash
# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
# Builds protobuf 3 for iOS(x86_64、i386、arm64、armv7、armv7s的支持)

set -e

if [[ -n MACOSX_DEPLOYMENT_TARGET ]]; then
    export MACOSX_DEPLOYMENT_TARGET=$(sw_vers -productVersion)
fi

SCRIPT_DIR=$(dirname $0)
source "${SCRIPT_DIR}/build_helper.subr"

cd tensorflow/contrib/makefile

HOST_GENDIR="$(pwd)/gen/protobuf-host"
mkdir -p "${HOST_GENDIR}"
if [[ ! -f "./downloads/protobuf/autogen.sh" ]]; then
    echo "You need to download dependencies before running this script." 1>&2
    echo "tensorflow/contrib/makefile/download_dependencies.sh" 1>&2
    exit 1
fi

JOB_COUNT="${JOB_COUNT:-$(get_job_count)}"

GENDIR=$(pwd)/gen/protobuf_ios/
LIBDIR=${GENDIR}lib
mkdir -p ${LIBDIR}

OSX_VERSION=darwin14.0.0

IPHONEOS_PLATFORM=$(xcrun --sdk iphoneos --show-sdk-platform-path)
IPHONEOS_SYSROOT=$(xcrun --sdk iphoneos --show-sdk-path)
IPHONESIMULATOR_PLATFORM=$(xcrun --sdk iphonesimulator --show-sdk-platform-path)
IPHONESIMULATOR_SYSROOT=$(xcrun --sdk iphonesimulator --show-sdk-path)
IOS_SDK_VERSION=$(xcrun --sdk iphoneos --show-sdk-version)
MIN_SDK_VERSION=8.0

CFLAGS="-DNDEBUG -Os -pipe -fPIC -fno-exceptions"
CXXFLAGS="${CFLAGS} -std=c++11 -stdlib=libc++"
LDFLAGS="-stdlib=libc++"
LIBS="-lc++ -lc++abi"

cd downloads/protobuf
PROTOC_PATH="${HOST_GENDIR}/bin/protoc"
if [[ ! -f "${PROTOC_PATH}" || ${clean} == true ]]; then
  # Try building compatible protoc first on host
  echo "protoc not found at ${PROTOC_PATH}. Build it first."
  make_host_protoc "${HOST_GENDIR}"
else
  echo "protoc found. Skip building host tools."
fi

./autogen.sh
if [ $? -ne 0 ]
then
  echo "./autogen.sh command failed."
  exit 1
fi

#编译x86_64
#make distclean
#./configure \
#--host=x86_64-apple-${OSX_VERSION} \
#--disable-shared \
#--enable-cross-compile \
#--with-protoc="${PROTOC_PATH}" \
#--prefix=${LIBDIR}/iossim_x86_64 \
#--exec-prefix=${LIBDIR}/iossim_x86_64 \
#"CFLAGS=${CFLAGS} \
#-mios-simulator-version-min=${MIN_SDK_VERSION} \
#-arch x86_64 \
#-fembed-bitcode \
#-isysroot ${IPHONESIMULATOR_SYSROOT}" \
#"CXX=${CXX}" \
#"CXXFLAGS=${CXXFLAGS} \
#-mios-simulator-version-min=${MIN_SDK_VERSION} \
#-arch x86_64 \
#-fembed-bitcode \
#-isysroot \
#${IPHONESIMULATOR_SYSROOT}" \
#LDFLAGS="-arch x86_64 \
#-fembed-bitcode \
#-mios-simulator-version-min=${MIN_SDK_VERSION} \
#${LDFLAGS} \
#-L${IPHONESIMULATOR_SYSROOT}/usr/lib/ \
#-L${IPHONESIMULATOR_SYSROOT}/usr/lib/system" \
#"LIBS=${LIBS}"
#make -j"${JOB_COUNT}"
#make install

#编译i386
#make distclean
#./configure \
#--host=i386-apple-${OSX_VERSION} \
#--disable-shared \
#--enable-cross-compile \
#--with-protoc="${PROTOC_PATH}" \
#--prefix=${LIBDIR}/iossim_i386 \
#--exec-prefix=${LIBDIR}/iossim_i386 \
#"CFLAGS=${CFLAGS} \
#-mios-simulator-version-min=${MIN_SDK_VERSION} \
#-arch i386 \
#-fembed-bitcode \
#-isysroot ${IPHONESIMULATOR_SYSROOT}" \
#"CXX=${CXX}" \
#"CXXFLAGS=${CXXFLAGS} \
#-mios-simulator-version-min=${MIN_SDK_VERSION} \
#-arch i386 \
#-fembed-bitcode \
#-isysroot \
#${IPHONESIMULATOR_SYSROOT}" \
#LDFLAGS="-arch i386 \
#-fembed-bitcode \
#-mios-simulator-version-min=${MIN_SDK_VERSION} \
#${LDFLAGS} \
#-L${IPHONESIMULATOR_SYSROOT}/usr/lib/ \
#-L${IPHONESIMULATOR_SYSROOT}/usr/lib/system" \
#"LIBS=${LIBS}"
#make -j"${JOB_COUNT}"
#make install

#编译arm64(包括iPhone6s | iphone6s plus｜iPhone6｜ iPhone6 plus｜iPhone5S | iPad Air｜ iPad mini2(iPad mini with Retina Display)
make distclean
./configure \
--host=arm \
--with-protoc="${PROTOC_PATH}" \
--disable-shared \
--prefix=${LIBDIR}/ios_arm64 \
--exec-prefix=${LIBDIR}/ios_arm64 \
"CFLAGS=${CFLAGS} \
-miphoneos-version-min=${MIN_SDK_VERSION} \
-arch arm64 \
-fembed-bitcode \
-isysroot ${IPHONEOS_SYSROOT}" \
"CXXFLAGS=${CXXFLAGS} \
-miphoneos-version-min=${MIN_SDK_VERSION} \
-arch arm64 \
-fembed-bitcode \
-isysroot ${IPHONEOS_SYSROOT}" \
LDFLAGS="-arch arm64 \
-fembed-bitcode \
-miphoneos-version-min=${MIN_SDK_VERSION} \
${LDFLAGS}" \
"LIBS=${LIBS}"
make -j"${JOB_COUNT}"
make install

#编译armv7(包括iPhone4｜iPhone4S｜iPad｜iPad2｜iPad3(The New iPad)｜iPad mini｜iPod Touch 3G｜iPod Touch4)
#make distclean
#./configure \
#--host=arm \
#--with-protoc="${PROTOC_PATH}" \
#--disable-shared \
#--prefix=${LIBDIR}/ios_armv7 \
#--exec-prefix=${LIBDIR}/ios_armv7 \
#"CFLAGS=${CFLAGS} \
#-miphoneos-version-min=${MIN_SDK_VERSION} \
#-arch armv7 \
#-fembed-bitcode \
#-isysroot ${IPHONEOS_SYSROOT}" \
#"CXXFLAGS=${CXXFLAGS} \
#-miphoneos-version-min=${MIN_SDK_VERSION} \
#-arch armv7 \
#-fembed-bitcode \
#-isysroot ${IPHONEOS_SYSROOT}" \
#LDFLAGS="-arch armv7 \
#-fembed-bitcode \
#-miphoneos-version-min=${MIN_SDK_VERSION} \
#${LDFLAGS}" \
#"LIBS=${LIBS}"
#make -j"${JOB_COUNT}"
#make install

#编译armv7s(包括iPhone5｜iPhone5C｜iPad4(iPad with Retina Display)
#make distclean
#./configure \
#--host=arm \
#--with-protoc="${PROTOC_PATH}" \
#--disable-shared \
#--prefix=${LIBDIR}/ios_armv7s \
#--exec-prefix=${LIBDIR}/ios_armv7s \
#"CFLAGS=${CFLAGS} \
#-miphoneos-version-min=${MIN_SDK_VERSION} \
#-arch armv7s \
#-fembed-bitcode \
#-isysroot ${IPHONEOS_SYSROOT}" \
#"CXXFLAGS=${CXXFLAGS} \
#-miphoneos-version-min=${MIN_SDK_VERSION} \
#-arch armv7s \
#-fembed-bitcode \
#-isysroot ${IPHONEOS_SYSROOT}" \
#LDFLAGS="-arch armv7s \
#-fembed-bitcode \
#-miphoneos-version-min=${MIN_SDK_VERSION} \
#${LDFLAGS}" \
#"LIBS=${LIBS}"
#make -j"${JOB_COUNT}"
#make install

#lipo \
#${LIBDIR}/iossim_x86_64/lib/libprotobuf.a \
#${LIBDIR}/iossim_i386/lib/libprotobuf.a \
#${LIBDIR}/ios_arm64/lib/libprotobuf.a \
#${LIBDIR}/ios_armv7/lib/libprotobuf.a \
#${LIBDIR}/ios_armv7s/lib/libprotobuf.a \
#-create \
#-output ${LIBDIR}/libprotobuf.a

lipo \
${LIBDIR}/ios_arm64/lib/libprotobuf.a \
-create \
-output ${LIBDIR}/libprotobuf.a

#lipo \
#${LIBDIR}/iossim_x86_64/lib/libprotobuf-lite.a \
#${LIBDIR}/iossim_i386/lib/libprotobuf-lite.a \
#${LIBDIR}/ios_arm64/lib/libprotobuf-lite.a \
#${LIBDIR}/ios_armv7/lib/libprotobuf-lite.a \
#${LIBDIR}/ios_armv7s/lib/libprotobuf-lite.a \
#-create \
#-output ${LIBDIR}/libprotobuf-lite.a

lipo \
${LIBDIR}/ios_arm64/lib/libprotobuf-lite.a \
-create \
-output ${LIBDIR}/libprotobuf-lite.a